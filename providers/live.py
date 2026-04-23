"""
Live providers — perform real network calls to NIH Reporter API, OpenAI Responses API, Scopus.
Use ReplayProviders for tests.
"""
import time
import threading
import re
from rapidfuzz import fuzz
import json
import requests
from datetime import datetime, timezone

from providers.base import (
    NihProvider, RolesProvider, HIndexProvider,
    Grant, RoleEvidence, HIndexEvidence,
)

_INSTITUTION_NIH_NAMES: dict[str, list[str]] = {
    "Stanford University School of Medicine":                                          ["STANFORD", "LUCILE SALTER PACKARD"],
    "University of California San Francisco School of Medicine":                       ["UNIVERSITY OF CALIFORNIA SAN FRANCISCO", "UCSF"],
    "Harvard Medical School":                                                          ["HARVARD", "BOSTON CHILDREN"],
    "Johns Hopkins School of Medicine":                                                ["JOHNS HOPKINS"],
    "University of Pennsylvania Perelman School of Medicine":                          ["UNIVERSITY OF PENNSYLVANIA", "CHILDREN'S HOSPITAL OF PHILADELPHIA"],
    "Columbia University Vagelos College of Physicians and Surgeons":                  ["COLUMBIA UNIVERSITY"],
    "Duke University School of Medicine":                                              ["DUKE UNIVERSITY"],
    "Washington University in St. Louis School of Medicine":                           ["WASHINGTON UNIVERSITY"],
    "University of Michigan Medical School":                                           ["UNIVERSITY OF MICHIGAN"],
    "Yale School of Medicine":                                                         ["YALE UNIVERSITY"],
    "University of Colorado School of Medicine / Children's Hospital Colorado":        ["UNIVERSITY OF COLORADO", "CHILDREN'S HOSPITAL COLORADO"],
    "University of Cincinnati College of Medicine / Cincinnati Children's":            ["UNIVERSITY OF CINCINNATI", "CINCINNATI CHILDREN"],
    "Baylor College of Medicine / Texas Children's Hospital":                          ["BAYLOR COLLEGE OF MEDICINE", "TEXAS CHILDREN"],
    "University of Washington School of Medicine / Seattle Children's":                ["UNIVERSITY OF WASHINGTON", "SEATTLE CHILDREN"],
    "Children's National Hospital / George Washington University":                     ["CHILDREN'S NATIONAL", "GEORGE WASHINGTON UNIVERSITY"],
    "Northwestern University Feinberg School of Medicine / Lurie Children's":          ["NORTHWESTERN UNIVERSITY", "LURIE CHILDREN"],
    "NIH National Institute of Child Health and Human Development (NICHD)":            ["NICHD", "NATIONAL INSTITUTE OF CHILD HEALTH"],
    "Keck School of Medicine USC / Children's Hospital Los Angeles":                   ["UNIVERSITY OF SOUTHERN CALIFORNIA", "CHILDREN'S HOSPITAL LOS ANGELES"],
    "Emory University School of Medicine / Children's Healthcare of Atlanta":          ["EMORY UNIVERSITY", "CHILDREN'S HEALTHCARE OF ATLANTA"],
    "University of Florida College of Medicine":                                       ["UNIVERSITY OF FLORIDA"],
    "Ohio State University College of Medicine / Nationwide Children's":               ["OHIO STATE UNIVERSITY", "NATIONWIDE CHILDREN"],
}

_INSTITUTION_KEYWORDS: dict[str, str] = {
    "Stanford University School of Medicine":                                          "stanford",
    "University of California San Francisco School of Medicine":                       "san francisco",
    "Harvard Medical School":                                                          "harvard",
    "Johns Hopkins School of Medicine":                                                "johns hopkins",
    "University of Pennsylvania Perelman School of Medicine":                          "pennsylvania",
    "Columbia University Vagelos College of Physicians and Surgeons":                  "columbia",
    "Duke University School of Medicine":                                              "duke",
    "Washington University in St. Louis School of Medicine":                           "washington university",
    "University of Michigan Medical School":                                           "michigan",
    "Yale School of Medicine":                                                         "yale",
    "University of Colorado School of Medicine / Children's Hospital Colorado":        "colorado",
    "University of Cincinnati College of Medicine / Cincinnati Children's":            "cincinnati",
    "Baylor College of Medicine / Texas Children's Hospital":                          "baylor",
    "University of Washington School of Medicine / Seattle Children's":                "washington",
    "Children's National Hospital / George Washington University":                     "children's national",
    "Northwestern University Feinberg School of Medicine / Lurie Children's":          "northwestern",
    "NIH National Institute of Child Health and Human Development (NICHD)":            "nichd",
    "Keck School of Medicine USC / Children's Hospital Los Angeles":                   "southern california",
    "Emory University School of Medicine / Children's Healthcare of Atlanta":          "emory",
    "University of Florida College of Medicine":                                       "florida",
    "Ohio State University College of Medicine / Nationwide Children's":               "ohio state",
}

_CREDENTIAL_SUFFIXES = frozenset(
    ["MD", "PhD", "DO", "MPH", "MBA", "FAAP", "MS", "DFAACAP", "RD", "RN"]
)

_LIVE_MODEL = "gpt-4o"

_ROLES_PROMPT = """\
Search the web for the professional roles and service activities of {name} at {school}.

Include ONLY roles that involve active service or responsibility beyond their faculty appointment:
- Editorial board positions (e.g. "Associate Editor, Diabetes Care")
- Professional society committee roles (e.g. "President, Pediatric Endocrine Society")
- Institutional or divisional leadership (e.g. "Division Chief", "Program Director", "Center Director")
- Training/education program roles (e.g. "Fellowship Program Director", "Clerkship Director")

Do NOT include:
- Academic titles or ranks (Professor, Assistant Professor, Instructor, Lecturer, Attending)
- General employment or affiliation (e.g. "Faculty, Stanford")
- Degrees or credentials (MD, PhD, etc.)
- Roles explicitly described as "former", "past", "previously", "emeritus", or "ex-"
- Roles with end dates clearly in the past (e.g. "(2011 - 2013)") — only include current/active roles

Respond ONLY with a JSON object with a single key "roles" containing an array.
Each item must have exactly these keys:
- "category": one of "editorial", "society", "leadership", "training"
- "role": the specific role title (e.g. "Associate Editor")
- "org": the organization or journal name (e.g. "Diabetes Care")
- "source_url": URL of the webpage where you found this
- "evidence_text": a RAW VERBATIM EXCERPT (up to 800 characters) copied word-for-word from the surrounding section of the source page where this role appears. Copy the full surrounding paragraph, list block, or table section — do NOT cherry-pick only the confirming sentence. Include enough context that a separate reviewer reading only this excerpt could independently confirm or deny the role claim. Do NOT paraphrase or summarize. If you cannot find raw source text, leave this field as an empty string.
- "retrieved_at": current UTC timestamp in ISO 8601 format

If no qualifying roles are found, return {{"roles": []}}.
No markdown fences, no explanation — only the JSON object.
"""


def _clean_name_parts(full_name: str) -> list[str]:
    parts = [p.strip(".,") for p in full_name.split() if p.strip(".,") not in _CREDENTIAL_SUFFIXES]
    return [p for p in parts if p]


def _parse_json(raw: str) -> dict:
    clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(clean)


import functools
import os

_NIH_SEARCH_URL = "https://api.reporter.nih.gov/v2/projects/search"
_NIH_FIELDS = ["project_num", "project_title", "fiscal_year", "organization", "principal_investigators"]
_NIH_CONFIDENCE_THRESHOLD = 0.50


# Suffixes stripped before searching OpenAlex (which indexes universities, not med schools)
_MED_SUFFIXES = (
    " school of medicine",
    " college of medicine",
    " medical school",
    " school of medicine and health sciences",
    " feinberg school of medicine",
    " perelman school of medicine",
    " vagelos college of physicians and surgeons",
    " keck school of medicine",
)


_OA_GENERIC = frozenset({
    "university", "the", "college", "of", "at", "a", "and",
    "school", "institute", "medical", "health",
})


def _openalex_fetch(search_term: str) -> list[dict]:
    resp = requests.get(
        "https://api.openalex.org/institutions",
        params={"search": search_term, "per_page": 25},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


def _distinctive_prefix(query: str) -> str:
    """Return the run of distinctive leading words from a query.

    Stops at the first generic word.  Examples:
      "Stanford University School of Medicine" → "Stanford"
      "Johns Hopkins University"               → "Johns Hopkins"
      "University of Michigan Medical School"  → ""  (starts generic)
    """
    words = query.split()
    prefix: list[str] = []
    for w in words:
        if w.lower() in _OA_GENERIC:
            break
        prefix.append(w)
    return " ".join(prefix)


def _openalex_search_one(query: str) -> list[str]:
    """Return lowercase alias list for a single OpenAlex institution search query.

    Strategy:
    - If the query starts with a distinctive prefix ("Stanford", "Johns Hopkins"),
      search OpenAlex by that prefix to get broad recall of all related entities,
      then filter results to those whose display_name contains the prefix.
    - If the query starts with a generic word ("University of …"), use the full
      query so OpenAlex ranking returns the correct specific institution; all
      returned results are included (the specific query already limits scope).
    """
    words = query.split()
    if not words:
        return []

    prefix = _distinctive_prefix(query)
    has_prefix = bool(prefix) and len(prefix) >= 4

    items = _openalex_fetch(prefix) if has_prefix else _openalex_fetch(query)
    if not items:
        return []

    if has_prefix:
        pattern = re.compile(r"\b" + re.escape(prefix.lower()) + r"\b")
    else:
        # Fall back to filtering by the first non-generic word
        filter_word = next(
            (w.lower() for w in words if w.lower() not in _OA_GENERIC),
            words[0].lower(),
        )
        pattern = re.compile(r"\b" + re.escape(filter_word) + r"\b")

    raw: list[str] = []
    for item in items:
        dn = item.get("display_name") or ""
        if not pattern.search(dn.lower()):
            continue
        raw.append(dn)
        raw += item.get("display_name_alternatives") or []
        if item.get("acronym"):
            raw.append(item["acronym"])

    return [a.lower() for a in raw if a]


def _candidate_queries(school: str) -> list[str]:
    """
    Build a list of OpenAlex search strings to try for a school name.

    Handles:
      - "Stanford University School of Medicine"          → "Stanford University"
      - "Baylor College of Medicine / Texas Children's"  → "Baylor College of Medicine"
                                                            "Texas Children's Hospital"
      - "Harvard Medical School"                         → "Harvard University"
    """
    candidates: list[str] = []
    # Split on " / " — some schools are listed as "A / B"
    parts = [p.strip() for p in school.split(" / ")]
    for part in parts:
        candidates.append(part)
        # Also try stripping known med-school suffixes to get the parent university
        lower = part.lower()
        for suffix in _MED_SUFFIXES:
            if lower.endswith(suffix):
                stripped = part[: len(part) - len(suffix)].strip()
                if stripped:
                    candidates.append(stripped)
                break
    # Deduplicate while preserving order
    seen: set[str] = set()
    return [c for c in candidates if not (c.lower() in seen or seen.add(c.lower()))]


_XLSX_PATH = os.path.join(os.path.dirname(__file__), "..", "medical_schools_lookup.xlsx")

_PAREN_RE = re.compile(r"\s*\([^)]+\)")


def _parse_hospital_field(raw: str) -> list[str]:
    """Split a hospital field on ';', strip parentheticals, return clean names."""
    names: list[str] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or part.lower().startswith("n/a"):
            continue
        paren = re.search(r"\(([^)]+)\)", part)
        base = _PAREN_RE.sub("", part).strip()
        if base:
            names.append(base)
        if paren:
            names.append(paren.group(1).strip())
    return names


@functools.lru_cache(maxsize=1)
def _load_xlsx_aliases() -> dict[str, frozenset[str]]:
    """
    Load medical_schools_lookup.xlsx into a dict:
      lowercase school name → frozenset of lowercase alias strings
    (school name + primary teaching hospital + all major affiliated hospitals).
    """
    try:
        import pandas as pd
        df = pd.read_excel(_XLSX_PATH, sheet_name="All Schools")
    except Exception as exc:
        print(f"[Lookup] Could not load medical_schools_lookup.xlsx: {exc}")
        return {}

    result: dict[str, frozenset[str]] = {}
    for _, row in df.iterrows():
        school = str(row.get("School Name") or "").strip()
        if not school or school == "nan":
            continue
        aliases: set[str] = {school.lower()}
        for col in ("Primary Teaching Hospital", "Major Affiliated Hospitals / Health Systems"):
            val = str(row.get(col) or "").strip()
            if val and val != "nan":
                for name in _parse_hospital_field(val):
                    aliases.add(name.lower())
        result[school.lower()] = frozenset(aliases)
    print(f"[Lookup] Loaded {len(result)} schools from xlsx")
    return result


def _xlsx_aliases_for(school: str) -> tuple[str, ...] | None:
    """
    Return alias tuple for a school from the xlsx lookup.
    Handles:
      - Exact match (case-insensitive)
      - Combined names with ' / ' — tries each part separately
      - Fuzzy fallback via token_set_ratio (threshold 75)
    Returns None if no confident match found.
    """
    from rapidfuzz import process as rp, fuzz as rf
    lookup = _load_xlsx_aliases()
    if not lookup:
        return None

    def _best_match(query: str) -> frozenset[str] | None:
        q = query.lower().strip()
        if q in lookup:
            return lookup[q]
        result = rp.extractOne(q, list(lookup.keys()), scorer=rf.token_set_ratio)
        if result and result[1] >= 75:
            return lookup[result[0]]
        return None

    # Try the full name first
    found = _best_match(school)
    if found:
        return tuple(found)

    # For combined names like "Baylor / Texas Children's", try each part
    if " / " in school:
        all_aliases: set[str] = set()
        for part in school.split(" / "):
            part_match = _best_match(part.strip())
            if part_match:
                all_aliases.update(part_match)
        if all_aliases:
            return tuple(all_aliases)

    return None


@functools.lru_cache(maxsize=64)
def _get_openalex_aliases(school: str) -> tuple[str, ...]:
    """
    Return all known institution name variants for NIH grant org matching.
    Primary source: medical_schools_lookup.xlsx (school + all affiliated hospitals).
    Fallback: OpenAlex API (for institutions not in the xlsx).
    Returns a deduplicated tuple of lowercase strings (cached per school).
    """
    # Try xlsx first
    xlsx_result = _xlsx_aliases_for(school)
    if xlsx_result:
        print(f"      [Lookup] Aliases for '{school}' ({len(xlsx_result)} from xlsx): {xlsx_result}")
        return xlsx_result

    # Fall back to OpenAlex
    print(f"      [OpenAlex] '{school}' not in xlsx — querying OpenAlex…")
    queries = _candidate_queries(school)
    all_aliases: list[str] = []
    seen: set[str] = set()

    for query in queries:
        try:
            aliases = _openalex_search_one(query)
            for a in aliases:
                if a not in seen:
                    seen.add(a)
                    all_aliases.append(a)
        except Exception as e:
            print(f"      [OpenAlex] Query '{query}' failed: {e}")

    if not all_aliases:
        all_aliases = [school.lower()]

    result = tuple(all_aliases)
    print(f"      [OpenAlex] Aliases for '{school}': {result}")
    return result


def _nih_search(payload: dict) -> list[dict]:
    r = requests.post(_NIH_SEARCH_URL, json=payload, timeout=20)
    r.raise_for_status()
    return r.json().get("results", [])


def _nih_fetch_all_by_profile(profile_id: int) -> list[dict]:
    """Paginate NIH Reporter to get every grant record for a given PI profile_id."""
    all_results: list[dict] = []
    offset = 0
    limit = 500
    while True:
        payload = {
            "criteria": {"pi_profile_ids": [profile_id]},
            "fields": ["project_num", "project_title", "fiscal_year", "organization"],
            "limit": limit,
            "offset": offset,
        }
        r = requests.post(_NIH_SEARCH_URL, json=payload, timeout=20)
        r.raise_for_status()
        data = r.json()
        batch = data.get("results", [])
        all_results.extend(batch)
        total = data.get("meta", {}).get("total", 0)
        offset += limit
        if offset >= total:
            break
    return all_results


class LiveNihProvider(NihProvider):
    def get_grants(self, name: str, school: str) -> list[Grant]:
        """
        Full flow:
          1. OpenAlex  — fetch all known institution name variants.
          2. NIH name search — query by first+last, no org filter, get
             principal_investigators.profile_id per result.
          3. Disambiguate — score each unique PI profile by:
               name similarity  (60 %) — fuzzy match of first + last name
               institution hit  (40 %) — any grant org matches an OpenAlex alias
          4. Accept best profile if score >= threshold; fallback to name
             distinctiveness for profiles with no institution match.
          5. Profile pull — query NIH by profile_id to get ALL grants
             (active + historical + cross-institution), paginating if needed.
        """
        parts = _clean_name_parts(name)
        if len(parts) < 2:
            return []
        first_name, last_name = parts[0], parts[-1]

        # Step 1 — institution aliases from OpenAlex
        aliases: tuple[str, ...] = _get_openalex_aliases(school)

        # Step 2 — name-based NIH search (no org filter)
        try:
            results = _nih_search({
                "criteria": {"pi_names": [{"first_name": first_name, "last_name": last_name}]},
                "fields": _NIH_FIELDS,
                "limit": 500,
                "offset": 0,
            })
        except Exception as e:
            print(f"      [NIH] Search failed for {name}: {e}")
            return []

        if not results:
            print(f"      [NIH] No results for {name}")
            return []

        print(f"      [NIH] Name search '{first_name} {last_name}' → {len(results)} candidate grant(s)")

        # Step 3 — build per-profile scoring map
        # profile_id → {pi_name, name_score, orgs, inst_hit}
        profiles: dict[int, dict] = {}
        for item in results:
            org_name = ((item.get("organization") or {}).get("org_name") or "").lower()
            for pi in (item.get("principal_investigators") or []):
                pid = pi.get("profile_id")
                if not pid:
                    continue
                if pid not in profiles:
                    pi_first = (pi.get("first_name") or "").strip().lower()
                    pi_last  = (pi.get("last_name")  or "").strip().lower()
                    first_sim = fuzz.ratio(first_name.lower(), pi_first) / 100.0
                    last_sim  = fuzz.ratio(last_name.lower(),  pi_last)  / 100.0
                    name_score = 0.3 * first_sim + 0.7 * last_sim
                    profiles[pid] = {
                        "pi_name": f"{pi.get('first_name','')} {pi.get('last_name','')}".strip(),
                        "name_score": name_score,
                        "orgs": set(),
                        "inst_hit": False,
                    }
                profiles[pid]["orgs"].add(org_name)

        # Institution match: any org in grant history hits an OpenAlex alias
        for prof in profiles.values():
            prof["inst_hit"] = any(
                alias in org
                for org in prof["orgs"]
                for alias in aliases
            )

        def _combined(prof: dict) -> float:
            return 0.6 * prof["name_score"] + 0.4 * (1.0 if prof["inst_hit"] else 0.0)

        best_pid  = max(profiles, key=lambda pid: _combined(profiles[pid]))
        best      = profiles[best_pid]
        score     = _combined(best)

        print(f"      [NIH] Best PI match: '{best['pi_name']}' "
              f"(score={score:.2f}, name={best['name_score']:.2f}, "
              f"inst={'yes' if best['inst_hit'] else 'no'})")

        # Step 4 — confidence gate
        if score < _NIH_CONFIDENCE_THRESHOLD:
            if not best["inst_hit"] and len(last_name) < 7:
                print(f"      [NIH] Low confidence + common name — skipping {name}")
                return []
            print(f"      [NIH] Low inst confidence but distinctive name — proceeding for {name}")

        # Step 5 — pull ALL grants by profile_id (active + historical)
        try:
            all_items = _nih_fetch_all_by_profile(best_pid)
        except Exception as e:
            print(f"      [NIH] Profile pull failed for profile_id={best_pid}: {e}")
            # Degrade gracefully: return what the name search already found
            all_items = results

        seen: set[str] = set()
        grants: list[Grant] = []
        for item in all_items:
            num = item.get("project_num", "Unknown")
            if num not in seen:
                seen.add(num)
                grants.append({"project_num": num, "title": item.get("project_title", "")})

        print(f"      [NIH] {name}: {len(grants)} total grant(s) "
              f"(profile_id={best_pid}, all institutions, all years)")
        return grants


class LiveRolesProvider(RolesProvider):
    def __init__(self, openai_client):
        self._client = openai_client

    def get_roles(self, name: str, school: str, profile_url: str = "") -> list[RoleEvidence]:
        prompt = _ROLES_PROMPT.format(name=name, school=school)
        response = self._client.responses.create(
            model=_LIVE_MODEL,
            tools=[{"type": "web_search_preview"}],
            input=[{"role": "user", "content": prompt}],
        )
        raw = ""
        for block in response.output:
            if hasattr(block, "content"):
                for item in block.content:
                    if hasattr(item, "text"):
                        raw += item.text

        parsed = _parse_json(raw)
        roles = parsed.get("roles", [])

        now = datetime.now(timezone.utc).isoformat()
        for role in roles:
            role.setdefault("retrieved_at", now)
            role.setdefault("model", _LIVE_MODEL)

        return roles


# ── Scoring helpers (used by LiveHIndexProvider) ──────────────────────────────

_GENERIC_WORDS = {
    "university", "universities", "medical", "center", "school", "hospital",
    "health", "institute", "system", "care", "college", "of", "and", "the",
    "at", "for", "children", "childrens", "general", "national", "research",
    "science", "sciences", "medicine", "clinic", "foundation", "department",
    "child", "maternal", "cancer", "stem", "cell", "biology", "regenerative"
}

_CREDENTIALS = {
    "MD", "PHD", "DO", "DDS", "DMD", "DVM", "PHARMD", "DRPH",
    "DPHIL", "MBBS", "MPH", "MS", "MSC", "MSN", "MHS", "MHA", "MBA",
    "FAAP", "FACS", "FACP", "FACE", "DFAACAP", "RN", "NP", "PA",
    "JR", "SR", "II", "III", "IV",
}

def _parse_name_for_scopus(full_name: str) -> tuple[str, str, str]:
    """Return (first, middle_initial, last) stripped of credentials.

    'Anna L. Gloyn, DPhil' → ('Anna', 'L', 'Gloyn')
    'Anna Gloyn'           → ('Anna', '',  'Gloyn')
    'José M. Rodriguez'    → ('José', 'M', 'Rodriguez')
    """
    name = full_name.split(",")[0].strip()
    parts = [p.strip(".,") for p in name.split() if p.strip(".,").upper() not in _CREDENTIALS and p.strip(".,")]
    if len(parts) < 2:
        return "", "", ""
    first = parts[0]
    last  = parts[-1]
    # middle initial: any single-letter part between first and last
    middle = next((p for p in parts[1:-1] if len(p) == 1), "")
    return first, middle, last


def _scopus_affil_str(entry: dict) -> str:
    affil = entry.get("affiliation-current", {})
    if isinstance(affil, list):
        return " ".join(a.get("affiliation-name", "") for a in affil)
    if isinstance(affil, dict):
        return affil.get("affiliation-name", "")
    return ""


def _affil_matches(affil_str: str, aliases: list[str]) -> bool:
    """True if any alias fuzzy-matches the affiliation string."""
    if not affil_str:
        return False
    affil = affil_str.lower()
    affil_words = set(re.findall(r"[a-zA-Z]+", affil))
    for alias in aliases:
        d_words = [w for w in re.findall(r"[a-zA-Z]+", alias.lower())
                   if w not in _GENERIC_WORDS and len(w) > 2]
        if not d_words:
            continue
        if any(w in affil_words for w in d_words) and fuzz.partial_ratio(alias.lower(), affil) >= 85:
            return True
    return False


def _name_matches(first: str, last: str, entry: dict) -> bool:
    """True if last name matches well and first name is consistent."""
    pref    = entry.get("preferred-name", {})
    s_first = pref.get("given-name", "").lower().strip()
    s_last  = pref.get("surname",    "").lower().strip()
    if fuzz.ratio(last.lower(), s_last) < 90:
        return False
    if not first or not s_first:
        return True
    # first initial must agree
    return s_first[0] == first[0].lower()


def _scopus_search(query: str, api_key: str) -> list[dict]:
    resp = requests.get(
        "https://api.elsevier.com/content/search/author",
        params={
            "query": query,
            "field": "dc:identifier,preferred-name,affiliation-current",
            "count": 25,
            "apiKey": api_key,
        },
        headers={"Accept": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    entries = resp.json().get("search-results", {}).get("entry", [])
    if not entries or "error" in entries[0]:
        return []
    return entries


def _scopus_fetch_profile(author_id: str, api_key: str) -> dict:
    resp = requests.get(
        f"https://api.elsevier.com/content/author/author_id/{author_id}",
        params={"field": "h-index,document-count,affiliation-current", "apiKey": api_key},
        headers={"Accept": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("author-retrieval-response", [{}])[0]


class LiveHIndexProvider(HIndexProvider):
    _lock = threading.Lock()
    _last_call = 0.0

    def __init__(self, scopus_api_key: str | None = None, discipline: str = "Pediatric Endocrinology"):
        self._api_key = scopus_api_key
        self._discipline = discipline

    def get_hindex(self, name: str, school: str) -> HIndexEvidence:
        with LiveHIndexProvider._lock:
            elapsed = time.time() - LiveHIndexProvider._last_call
            if elapsed < 1.5:
                time.sleep(1.5 - elapsed)
            LiveHIndexProvider._last_call = time.time()

        now = datetime.now(timezone.utc).isoformat()

        if not self._api_key:
            return {
                "h_index": None, "scopus_author_id": None,
                "source_url": "", "evidence_text": "Scopus API key not set — h-index skipped",
                "retrieved_at": now,
            }

        first, middle, last = _parse_name_for_scopus(name)
        if not first or not last:
            return {
                "h_index": None, "scopus_author_id": None,
                "source_url": "", "evidence_text": f"Could not parse name: {name}",
                "retrieved_at": now,
            }

        # Step 1: Search — try with middle initial first, fall back to first only
        entries = []
        for q in ([f"AUTHLASTNAME({last}) AND AUTHFIRST({first} {middle})"] if middle else []) + \
                  [f"AUTHLASTNAME({last}) AND AUTHFIRST({first})"]:
            print(f"      [Scopus] Querying: {q}")
            entries = _scopus_search(q, self._api_key)
            if entries:
                break

        if not entries:
            return {
                "h_index": None, "scopus_author_id": None,
                "source_url": "", "evidence_text": f"No Scopus results for {name}",
                "retrieved_at": now,
            }

        # Step 2: Filter to name matches only
        name_matches = [e for e in entries if _name_matches(first, last, e)]
        if not name_matches:
            return {
                "h_index": None, "scopus_author_id": None,
                "source_url": "", "evidence_text": f"No name match found in Scopus for {name}",
                "retrieved_at": now,
            }

        xlsx_aliases = _xlsx_aliases_for(school)
        aliases = list(xlsx_aliases) if xlsx_aliases else [school.lower()]

        # Step 3: Pick best candidate
        # First preference: search result already shows a matching affiliation
        affil_match = next((e for e in name_matches if _affil_matches(_scopus_affil_str(e), aliases)), None)
        if affil_match:
            best_entry = affil_match
            confidence = "high"
            print(f"      [Scopus] Affiliation match in search results")
        elif len(name_matches) == 1:
            # One name match — fetch their full profile to check current affiliation
            # (search index is often stale; profile reflects current institution)
            best_entry = name_matches[0]
            confidence = "needs_review"
            pref = best_entry.get("preferred-name", {})
            display = f"{pref.get('given-name','')} {pref.get('surname','')}".strip()
            print(f"      [Scopus] Single name match '{display}' — will verify via profile")
        else:
            # Multiple name matches, none with matching affiliation in search results
            # Try fetching profiles for each to find the one at this institution
            best_entry = None
            confidence = "needs_review"
            for candidate in name_matches:
                cid = candidate.get("dc:identifier", "").replace("AUTHOR_ID:", "")
                if not cid:
                    continue
                try:
                    prof = _scopus_fetch_profile(cid, self._api_key)
                    affil_data = prof.get("author-profile", {}).get("affiliation-current", {})
                    affil_name = ""
                    if isinstance(affil_data, dict):
                        affil_name = (affil_data.get("affiliation", {}) or {}).get("ip-doc", {}).get("sort-name", "")
                    if _affil_matches(affil_name, aliases):
                        best_entry = candidate
                        confidence = "high"
                        print(f"      [Scopus] Found affiliation match via profile fetch")
                        break
                except Exception:
                    continue
            if best_entry is None:
                pref = name_matches[0].get("preferred-name", {})
                display = f"{pref.get('given-name','')} {pref.get('surname','')}".strip()
                print(f"      [Scopus] Multiple matches for '{last}', no affiliation confirmed — skipping. Top: '{display}'")
                return {
                    "h_index": None, "scopus_author_id": None,
                    "source_url": "", "evidence_text": "Multiple name matches, no affiliation confirmed — skipped",
                    "retrieved_at": now,
                }

        author_id = best_entry.get("dc:identifier", "").replace("AUTHOR_ID:", "")
        if not author_id:
            return {
                "h_index": None, "scopus_author_id": None,
                "source_url": "", "evidence_text": "Could not extract Scopus author ID",
                "retrieved_at": now,
            }

        # Step 4: Fetch full profile for h-index (also has current affiliation)
        print(f"      [Scopus] Fetching profile for author_id={author_id}")
        profile_data  = _scopus_fetch_profile(author_id, self._api_key)
        h_index_val   = profile_data.get("h-index")
        doc_count     = profile_data.get("coredata", {}).get("document-count", "?")
        h_index       = int(h_index_val) if h_index_val is not None else None

        # If search showed stale affiliation, use profile's current affiliation for display
        profile_affil = ""
        try:
            affil_data   = profile_data.get("author-profile", {}).get("affiliation-current", {})
            profile_affil = (affil_data.get("affiliation", {}) or {}).get("ip-doc", {}).get("sort-name", "")
        except Exception:
            pass
        display_affil = profile_affil or _scopus_affil_str(best_entry)

        # Upgrade confidence if profile affiliation now matches
        if confidence == "needs_review" and _affil_matches(profile_affil, aliases):
            confidence = "high"

        pref    = best_entry.get("preferred-name", {})
        display = f"{pref.get('given-name','')} {pref.get('surname','')}".strip()
        print(f"      [Scopus] Matched '{display}' @ '{display_affil}' (confidence={confidence}, h-index={h_index})")

        return {
            "h_index": h_index,
            "scopus_author_id": author_id,
            "source_url": f"https://www.scopus.com/authid/detail.uri?authorId={author_id}",
            "evidence_text": f"Scopus: h-index={h_index}, documents={doc_count}, affil='{display_affil}', confidence={confidence}",
            "retrieved_at": now,
        }
