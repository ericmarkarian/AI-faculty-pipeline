"""
Institution resolver — automatically discovers the correct NIH Reporter org-name
substrings and Scopus affiliation aliases for any school, then caches results.

Resolution order:
  1. Hardcoded overrides in providers/institutions.py  (manually verified, always wins)
  2. Persistent JSON cache  data/institution_names_cache.json  (from a previous run)
  3. Candidate generation   parse the school name string into plausible substrings
  4. NIH probe              fire a limit-1 query per candidate; keep all that return ≥1 result
  5. Fallback               use the raw school name uppercased if everything fails

Scopus aliases are not probed against the API — generated candidates are trusted
because Scopus affiliation matching is fuzzy and a near-miss still beats the old
"use the full school name" fallback.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import requests

from providers.institutions import INSTITUTION_NIH_NAMES, INSTITUTION_KEYWORDS

_CACHE_PATH = Path(__file__).parent.parent / "data" / "institution_names_cache.json"
_CACHE_LOCK = threading.Lock()

_NIH_API     = "https://api.reporter.nih.gov/v2/projects/search"
_NIH_TIMEOUT = 10   # seconds per probe


# ── Name-cleaning patterns (applied longest-first to avoid partial strips) ────

_MED_STRIPS: list[str] = [
    r"\bVagelos College of Physicians and Surgeons\b",
    r"\bFeinberg School of Medicine\b",
    r"\bPerelman School of Medicine\b",
    r"\bDavid Geffen School of Medicine at\b",
    r"\bKeck School of Medicine\b",
    r"\bCollege of Physicians and Surgeons\b",
    r"\bSchool of Medicine\b",
    r"\bCollege of Medicine\b",
    r"\bMedical School\b",
    r"\bSchool of Public Health\b",
    r"\bfor Medical Sciences\b",
]

# Leading connector words left behind after a med-school strip
# e.g. "Perelman School of Medicine at the University of PA" → "at the University of PA"
_LEADING_CONNECTOR = re.compile(r"^(?:at\s+the\s+|at\s+|of\s+the\s+|of\s+)", re.IGNORECASE)

# Expand known abbreviations to the full NIH-style org name (ALL CAPS)
_ABBREV_EXPAND: dict[str, str] = {
    "UCLA":  "UNIVERSITY OF CALIFORNIA LOS ANGELES",
    "UCSF":  "UNIVERSITY OF CALIFORNIA SAN FRANCISCO",
    "USC":   "UNIVERSITY OF SOUTHERN CALIFORNIA",
    "NICHD": "NATIONAL INSTITUTE OF CHILD HEALTH AND HUMAN DEVELOPMENT",
    "NIH":   "NATIONAL INSTITUTES OF HEALTH",
}


# ── Candidate generators ──────────────────────────────────────────────────────

def _strip(text: str, patterns: list[str]) -> str:
    for pat in patterns:
        text = re.sub(pat, "", text, flags=re.IGNORECASE)
    return text.strip(" ,/")


def _generate_nih_candidates(school: str) -> list[str]:
    """Return NIH org-name substrings (ALL CAPS), most specific first, deduplicated."""
    seen: set[str] = set()
    out: list[str] = []

    def add(s: str) -> None:
        s = s.strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)

    # Joint programs ("X / Y") — try each half separately
    parts = [p.strip() for p in school.split("/")]

    for part in parts:
        # Direct abbreviation expansion (e.g. "UCLA" alone)
        if part.strip().upper() in _ABBREV_EXPAND:
            add(_ABBREV_EXPAND[part.strip().upper()])
            add(part)           # also try the abbreviation itself
            continue

        # 1. Whole segment as-is
        add(part)

        # 2. Strip med-school qualifier
        stripped = _strip(part, _MED_STRIPS)
        # Also strip any leading connector words left behind (e.g. "at the")
        stripped = _LEADING_CONNECTOR.sub("", stripped).strip(" ,/")
        if stripped and stripped.upper() != part.upper():
            # 2a. Try expanding if the remainder is an abbreviation
            if stripped.strip().upper() in _ABBREV_EXPAND:
                add(_ABBREV_EXPAND[stripped.strip().upper()])
            add(stripped)

        # 3. Strip "Children's Hospital" variants from the original — gives a
        #    clean university name that often matches NIH better than the full string
        hosp_stripped = re.sub(
            r"\bChildren['']?s(?:\s+Hospital(?:\s+\w+)*)?\b", "", part, flags=re.IGNORECASE
        ).strip(" ,/")
        if hosp_stripped and hosp_stripped.upper() != part.upper():
            add(hosp_stripped)

    return out


def _generate_scopus_aliases(school: str) -> list[str]:
    """Return Scopus affiliation keywords (lowercase), best first, deduplicated."""
    seen: set[str] = set()
    out: list[str] = []

    def add(s: str) -> None:
        s = s.strip().lower()
        if s and s not in seen:
            seen.add(s)
            out.append(s)

    parts = [p.strip() for p in school.split("/")]

    for part in parts:
        upper = part.strip().upper()

        # Expand abbreviations → gives a rich alias like "university of california los angeles"
        if upper in _ABBREV_EXPAND:
            add(_ABBREV_EXPAND[upper])
            add(part)
            continue

        # Strip med-school suffix → keeps the meaningful university/hospital name
        stripped = _strip(part, _MED_STRIPS)
        stripped = _LEADING_CONNECTOR.sub("", stripped).strip(" ,/")
        if stripped:
            exp = _ABBREV_EXPAND.get(stripped.strip().upper())
            if exp:
                add(exp)
            else:
                add(stripped)

        # Whole part as fallback
        add(part)

    return out


# ── Cache I/O (always called inside _CACHE_LOCK) ─────────────────────────────

def _load_cache(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"nih": {}, "scopus": {}}


def _save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2))


# ── NIH probe ────────────────────────────────────────────────────────────────

def _probe_nih(candidate: str) -> bool:
    """Return True if NIH Reporter returns ≥1 result for this org-name substring."""
    try:
        r = requests.post(
            _NIH_API,
            json={"criteria": {"org_names": [candidate]}, "limit": 1, "offset": 0},
            timeout=_NIH_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("meta", {}).get("total", 0) > 0
    except Exception:
        return False


# ── Public API ────────────────────────────────────────────────────────────────

def get_nih_org_names(school: str, cache_path: Path = _CACHE_PATH) -> list[str]:
    """
    Return working NIH org-name substrings for *school*.

    Result is a list because some schools have grants split across two orgs
    (e.g. a university AND its affiliated children's hospital).
    """
    # 1. Hardcoded override — always authoritative
    if school in INSTITUTION_NIH_NAMES:
        return INSTITUTION_NIH_NAMES[school]

    # 2. Persistent cache
    with _CACHE_LOCK:
        cache = _load_cache(cache_path)
        cached = cache.get("nih", {}).get(school)

    if cached is not None:
        # Empty list means we probed before and found nothing — use fallback
        return cached if cached else _nih_fallback(school)

    # 3. Generate + probe (outside the lock — HTTP calls can be slow)
    candidates = _generate_nih_candidates(school)
    print(f"    [InstitutionResolver] NIH probe for {school!r}")
    print(f"    [InstitutionResolver] Candidates: {candidates}")

    winners = [c for c in candidates if _probe_nih(c)]
    print(f"    [InstitutionResolver] Confirmed: {winners or '(none — using fallback)'}")

    # 4. Write back to cache
    with _CACHE_LOCK:
        cache = _load_cache(cache_path)
        cache.setdefault("nih", {})[school] = winners
        _save_cache(cache_path, cache)

    return winners if winners else _nih_fallback(school)


def _nih_fallback(school: str) -> list[str]:
    """Last resort when probing finds nothing — avoids a silent empty result."""
    return [school.upper().split("/")[0].strip()]


def get_scopus_aliases(school: str, cache_path: Path = _CACHE_PATH) -> list[str]:
    """
    Return Scopus affiliation keywords (lowercase) for *school*.

    These are used as fuzzy-match candidates in _affiliation_score — we don't
    probe Scopus because the search is fuzzy and near-misses still help.
    """
    # 1. Hardcoded override
    if school in INSTITUTION_KEYWORDS:
        return [INSTITUTION_KEYWORDS[school]]

    # 2. Persistent cache
    with _CACHE_LOCK:
        cache = _load_cache(cache_path)
        cached = cache.get("scopus", {}).get(school)

    if cached is not None:
        return cached

    # 3. Generate and cache
    aliases = _generate_scopus_aliases(school)
    print(f"    [InstitutionResolver] Scopus aliases for {school!r}: {aliases}")

    with _CACHE_LOCK:
        cache = _load_cache(cache_path)
        cache.setdefault("scopus", {})[school] = aliases
        _save_cache(cache_path, cache)

    return aliases
