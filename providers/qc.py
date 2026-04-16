"""
providers/qc.py — Deterministic triple-scorer for faculty role claim verification.

For each role collected by Agent 2 (editorial, society, leadership, training):
  Scores three independent signals against the stored evidence_text:
    1. Role match   — do the key words of the role title appear in the evidence?
    2. Org match    — does the org name appear in the evidence?
    3. Page identity — does this evidence actually belong to the right person?
       - Checks name anywhere in evidence text (page-level, not per-entry)
       - OR checks name slug in source_url
       - If neither: evidence may be from the wrong person's page → penalised

  No LLM calls. No API cost. Fully deterministic.

Per-role confidence (0.0–1.0) maps to:
  green  — ≥ 0.75
  yellow — 0.40–0.74
  red    — < 0.40

Per-person overall: worst status across all roles in all categories.
"""

import re
from rapidfuzz import fuzz

ROLE_CATEGORIES = ("editorial", "society", "leadership", "training")

_CREDENTIALS = frozenset({
    "MD", "PHD", "DO", "DPHIL", "MBBS", "MPH", "MS", "MSC", "MHS", "MBA",
    "FAAP", "FACS", "FACP", "FACE", "RN", "NP", "PA", "JR", "SR", "II", "III", "IV",
})

_STOP_WORDS = frozenset({
    "of", "the", "and", "at", "for", "in", "a", "an", "to", "with", "by",
})

# Org words that are too generic to be meaningful match signals on their own
_ORG_STOP_WORDS = _STOP_WORDS | frozenset({
    "university", "school", "college", "institute", "hospital", "center",
    "centre", "medical", "health", "system", "national", "american", "society",
    "association", "foundation", "department", "division",
})

GREEN_THRESHOLD  = 0.75
YELLOW_THRESHOLD = 0.40


# ── Name helpers ──────────────────────────────────────────────────────────────

def _clean_name_parts(full_name: str) -> list[str]:
    """Return name tokens with credentials stripped."""
    return [
        p.strip(".,")
        for p in full_name.split()
        if p.strip(".,").upper() not in _CREDENTIALS and p.strip(".,")
    ]


def _name_variants(full_name: str) -> list[str]:
    """Return plausible surface forms of the name to search for."""
    parts = _clean_name_parts(full_name)
    if not parts:
        return [full_name]
    variants = []
    variants.append(" ".join(parts))                          # Darrell M. Wilson
    if len(parts) >= 2:
        variants.append(f"{parts[0]} {parts[-1]}")            # Darrell Wilson
        variants.append(f"{parts[0][0]}. {parts[-1]}")        # D. Wilson
        variants.append(f"{parts[0][0]}{parts[-1]}")          # DWilson (URL slug)
        variants.append(parts[-1])                            # Wilson (last only — low signal)
    return variants


def _name_in_url(full_name: str, url: str) -> bool:
    """True if the person's last name (and ideally first initial) appear in the URL."""
    if not url:
        return False
    url_lower = url.lower()
    parts = [p.lower() for p in _clean_name_parts(full_name)]
    if not parts:
        return False
    last = parts[-1]
    if last not in url_lower:
        return False
    # Require at least first initial too, to avoid false positives on common last names
    if len(parts) >= 2 and parts[0][0] in url_lower:
        return True
    # If last name is distinctive (>7 chars), first initial alone is enough
    return len(last) > 7


def _name_in_evidence(full_name: str, evidence: str) -> bool:
    """
    True if a plausible variant of the person's name appears anywhere in the
    evidence text. Uses fuzzy matching to handle typos (e.g. 'Glyn' → 'Gloyn').
    Last-name-only hits are not counted (too much ambiguity).
    """
    if not evidence:
        return False
    evidence_lower = evidence.lower()
    parts = _clean_name_parts(full_name)

    for variant in _name_variants(full_name):
        if len(variant) <= len(parts[-1] if parts else variant):
            continue  # skip last-name-only variants here
        v_lower = variant.lower()
        if v_lower in evidence_lower:
            return True
        # Fuzzy: allow 1-2 character edits (covers 'Glyn' vs 'Gloyn')
        if len(v_lower) >= 6 and fuzz.partial_ratio(v_lower, evidence_lower) >= 88:
            return True
    return False


# ── Scoring helpers ───────────────────────────────────────────────────────────

def _token_overlap(query: str, text: str, stop_words: frozenset) -> float:
    """
    Fraction of meaningful tokens in `query` that appear in `text`.
    Returns 0.0 if no meaningful tokens exist.
    """
    tokens = [
        t.lower() for t in re.findall(r"[a-zA-Z]+", query)
        if t.lower() not in stop_words and len(t) > 2
    ]
    if not tokens:
        return 0.0
    text_lower = text.lower()
    matched = sum(1 for t in tokens if t in text_lower)
    return matched / len(tokens)


def _negation_penalty(role: str, evidence: str) -> float:
    """
    Return a penalty (0.0–0.35) if the evidence suggests the role is past/negated.
    Looks for explicit negation phrases or clearly closed date ranges near the role.
    """
    if not evidence:
        return 0.0
    ev = evidence.lower()

    negation_phrases = [
        r"\bformer\b", r"\bpreviously\b", r"\bno longer\b",
        r"\bstepped down\b", r"\bretired\b", r"\bpast\b\s+(?:chair|president|director|chief)",
    ]
    for pat in negation_phrases:
        if re.search(pat, ev):
            return 0.35

    # Closed date range (e.g. "2001-2008", "Sep 1990-Aug 2000") ending before 2020
    role_tokens = [t.lower() for t in re.findall(r"[a-zA-Z]+", role) if len(t) > 2]
    closed_range = re.search(
        r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+)?(\d{4})\s*[-–]\s*"
        r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+)?(\d{4})\b",
        ev,
        re.IGNORECASE,
    )
    if closed_range:
        end_year = int(closed_range.group(4))
        if end_year < 2020:
            # Only penalise if this date range sits near the role title in the evidence
            range_pos = closed_range.start()
            for tok in role_tokens:
                tok_pos = ev.find(tok)
                if tok_pos >= 0 and abs(tok_pos - range_pos) < 250:
                    return 0.25

    return 0.0


# ── Main claim scorer ─────────────────────────────────────────────────────────

def score_claim(
    name: str,
    role: str,
    org: str,
    evidence_text: str,
    source_url: str,
) -> tuple[float, dict]:
    """
    Score a single role claim against its stored evidence text.

    Returns (confidence: float 0–1, breakdown: dict).

    Confidence thresholds:
      ≥ 0.75 → green   (well-supported)
      0.40–0.74 → yellow (partial support)
      < 0.40  → red    (unsupported or wrong page)
    """
    evidence = (evidence_text or "").strip()

    # ── Step 1: confirm the page belongs to the right person ──────────────────
    name_in_ev  = _name_in_evidence(name, evidence)
    name_in_url = _name_in_url(name, source_url or "")
    page_is_theirs = name_in_ev or name_in_url

    # ── Step 2: role and org token overlap ────────────────────────────────────
    role_s = _token_overlap(role, evidence, _STOP_WORDS)
    org_s  = _token_overlap(org,  evidence, _ORG_STOP_WORDS)

    # ── Step 3: negation / temporal penalty ───────────────────────────────────
    neg = _negation_penalty(role, evidence)

    # ── Step 4: combine ───────────────────────────────────────────────────────
    if not evidence:
        # No evidence captured at all
        raw = 0.0
    elif page_is_theirs:
        # Page confirmed as belonging to this person — evaluate role + org only
        raw = 0.60 * role_s + 0.40 * org_s
    else:
        # Can't confirm whose page this is — heavily penalise
        # Role/org match alone is weak signal (could be any page mentioning the org)
        raw = 0.40 * role_s + 0.30 * org_s
        # Cap at yellow even with perfect role+org match, since we can't confirm identity
        raw = min(raw, YELLOW_THRESHOLD + 0.05)

    confidence = max(0.0, min(1.0, raw - neg))

    breakdown = {
        "role_score":      round(role_s, 3),
        "org_score":       round(org_s, 3),
        "name_in_evidence": name_in_ev,
        "name_in_url":     name_in_url,
        "page_is_theirs":  page_is_theirs,
        "negation_penalty": round(neg, 3),
        "confidence":      round(confidence, 3),
    }
    return confidence, breakdown


def _confidence_to_status(confidence: float) -> str:
    if confidence >= GREEN_THRESHOLD:
        return "green"
    if confidence >= YELLOW_THRESHOLD:
        return "yellow"
    return "red"


# ── Aggregation ───────────────────────────────────────────────────────────────

def _worst(statuses: list[str]) -> str:
    if "red" in statuses:
        return "red"
    if "yellow" in statuses:
        return "yellow"
    if "green" in statuses:
        return "green"
    return "green"


def verify_faculty_roles(faculty: dict, client=None) -> dict:
    """
    Scores all role claims for one faculty member against their stored evidence.
    `client` is accepted for API compatibility but is no longer used.

    Returns a qc dict with per-category and overall confidence.

    Shape:
    {
      "overall":               "green" | "yellow" | "red",
      "editorial_confidence":  "green" | "yellow" | "red",
      "society_confidence":    "green" | "yellow" | "red",
      "leadership_confidence": "green" | "yellow" | "red",
      "training_confidence":   "green" | "yellow" | "red",
      "role_details": [
        {
          "category":    "editorial",
          "role":        "Associate Editor",
          "org":         "Diabetes Care",
          "confidence":  0.91,
          "status":      "green",
          "breakdown":   { ... },
        },
        ...
      ]
    }
    """
    name = faculty.get("full_name", "Unknown")

    all_roles: list[dict] = []
    for cat in ROLE_CATEGORIES:
        for role in (faculty.get(f"{cat}_roles") or []):
            if isinstance(role, dict):
                all_roles.append({**role, "_cat": cat})

    if not all_roles:
        empty = {f"{cat}_confidence": "green" for cat in ROLE_CATEGORIES}
        return {"overall": "green", "role_details": [], **empty}

    print(f"    [QC] {name} — scoring {len(all_roles)} role(s)")

    role_details = []
    category_statuses: dict[str, list[str]] = {cat: [] for cat in ROLE_CATEGORIES}

    for r in all_roles:
        cat        = r.get("_cat", "editorial")
        role_title = r.get("role", "")
        org        = r.get("org", "")
        evidence   = r.get("evidence_text", "")
        source_url = r.get("source_url", "")

        confidence, breakdown = score_claim(name, role_title, org, evidence, source_url)
        status = _confidence_to_status(confidence)
        category_statuses[cat].append(status)

        evidence_preview = (evidence[:120] + "...") if len(evidence) > 120 else evidence or "(none)"
        print(
            f"           [{status.upper()}] conf={confidence:.2f} "
            f"(role={breakdown['role_score']}, org={breakdown['org_score']}, "
            f"page={'YES' if breakdown['page_is_theirs'] else 'NO'}, "
            f"neg={breakdown['negation_penalty']}) "
            f"{cat}: \"{role_title}\" @ \"{org}\""
        )
        print(f"                     evidence: {evidence_preview}")

        role_details.append({
            "category":   cat,
            "role":       role_title,
            "org":        org,
            "confidence": confidence,
            "status":     status,
            "breakdown":  breakdown,
        })

    cat_confidences = {
        f"{cat}_confidence": _worst(category_statuses[cat]) if category_statuses[cat] else "green"
        for cat in ROLE_CATEGORIES
    }
    overall = _worst(list(cat_confidences.values()))

    print(
        f"    [QC] {name} summary — "
        + " | ".join(f"{cat}: {cat_confidences[f'{cat}_confidence']}" for cat in ROLE_CATEGORIES)
        + f" | OVERALL: {overall.upper()}"
    )

    return {
        "overall": overall,
        **cat_confidences,
        "role_details": role_details,
    }
