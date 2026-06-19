"""
Live providers — perform real network calls to NIH Reporter API, OpenAI Responses API, Scopus.
Use ReplayProviders for tests.
"""
import time
import threading
import re
from pathlib import Path
from rapidfuzz import fuzz
import json
import requests
from datetime import datetime, timezone

from providers.base import (
    NihProvider, RolesProvider, HIndexProvider,
    Grant, RoleEvidence, HIndexEvidence,
)
from providers.utils import clean_url

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
    "UC San Diego School of Medicine / Rady Children's Hospital":                      ["UNIVERSITY OF CALIFORNIA SAN DIEGO", "RADY CHILDREN"],
    "University of Pittsburgh School of Medicine / UPMC Children's Hospital":          ["UNIVERSITY OF PITTSBURGH", "UPMC", "CHILDREN'S HOSPITAL OF PITTSBURGH"],
    "David Geffen School of Medicine at UCLA / Mattel Children's Hospital":            ["UNIVERSITY OF CALIFORNIA LOS ANGELES", "UCLA", "MATTEL CHILDREN"],
    "Vanderbilt University School of Medicine / Monroe Carell Jr. Children's":         ["VANDERBILT UNIVERSITY", "MONROE CARELL"],
    "Medical University of South Carolina":                                            ["MEDICAL UNIVERSITY OF SOUTH CAROLINA", "MUSC"],
    "UT Southwestern Medical Center / Children's Medical Center Dallas":               ["UNIVERSITY OF TEXAS SOUTHWESTERN", "UT SOUTHWESTERN", "CHILDREN'S MEDICAL CENTER DALLAS"],
    "Dell Medical School, University of Texas at Austin / Dell Children's":            ["UNIVERSITY OF TEXAS AT AUSTIN", "DELL CHILDREN"],
    "University of Utah School of Medicine / Intermountain Primary Children's":        ["UNIVERSITY OF UTAH", "INTERMOUNTAIN", "PRIMARY CHILDREN"],
    "University of Missouri-Kansas City School of Medicine / Children's Mercy":        ["UNIVERSITY OF MISSOURI KANSAS CITY", "CHILDREN'S MERCY"],
    "Nemours Children's Health":                                                       ["NEMOURS", "ALFRED I. DUPONT"],
    "Atrium Health / Levine Children's Hospital":                                      ["ATRIUM HEALTH", "LEVINE CHILDREN", "CAROLINAS MEDICAL"],
    "Mayo Clinic Alix School of Medicine":                                             ["MAYO CLINIC", "MAYO FOUNDATION"],
    "Cleveland Clinic Lerner College of Medicine":                                     ["CLEVELAND CLINIC", "LERNER COLLEGE"],
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
    "UC San Diego School of Medicine / Rady Children's Hospital":                      "rady children",
    "University of Pittsburgh School of Medicine / UPMC Children's Hospital":          "pittsburgh",
    "David Geffen School of Medicine at UCLA / Mattel Children's Hospital":            "los angeles",
    "Vanderbilt University School of Medicine / Monroe Carell Jr. Children's":         "vanderbilt",
    "Medical University of South Carolina":                                            "south carolina",
    "UT Southwestern Medical Center / Children's Medical Center Dallas":               "southwestern",
    "Dell Medical School, University of Texas at Austin / Dell Children's":            "texas austin",
    "University of Utah School of Medicine / Intermountain Primary Children's":        "utah",
    "University of Missouri-Kansas City School of Medicine / Children's Mercy":        "children's mercy",
    "Nemours Children's Health":                                                       "nemours",
    "Atrium Health / Levine Children's Hospital":                                      "atrium",
    "Mayo Clinic Alix School of Medicine":                                             "mayo",
    "Cleveland Clinic Lerner College of Medicine":                                     "cleveland clinic",
    "Children's National Hospital / George Washington University":                     "george washington",
}


_LIVE_MODEL = "gpt-4o"

_ROLES_DIR = Path(__file__).parent.parent / "data" / "roles"


def _load_discipline_context(discipline: str) -> str:
    filename = discipline.lower().replace(" ", "_").replace("/", "_") + ".md"
    path = _ROLES_DIR / filename
    if not path.exists():
        return ""
    content = path.read_text().strip()
    return f"--- Specialty guidance for {discipline} ---\n{content}\n---\n\n"


_MERGE_PROMPT = """\
You maintain a specialty guidance file used to direct an AI agent searching for professional \
roles of academic faculty. The file lists target journals, societies, and leadership roles \
relevant to the specialty.

Current file content:
{existing}

New input from a physician in this specialty:
{user_input}

Task: Produce an updated version of the file that incorporates genuinely new information:
- Add new journals at the TOP of the "Target Journals" section
- Add new societies at the TOP of the "Target Societies" section
- Add new leadership roles at the TOP of the "Leadership Roles" section
- Do NOT duplicate anything already present (check carefully before adding)
- Preserve the existing markdown structure and headings exactly
- Do NOT remove or reorder existing content

Return ONLY the updated file content. No explanation, no markdown fences.
"""

_MERGE_PROMPT_NEW = """\
Create a specialty guidance file for {discipline} faculty role searching based on this \
physician input: {user_input}

Structure it with these sections (markdown format):
# Role Search Config: {discipline}
## Target Societies
## Target Journals
## Leadership Roles

Return ONLY the file content. No explanation, no markdown fences.
"""


def merge_specialty_considerations(discipline: str, user_input: str, openai_client) -> None:
    """Merge physician input into the specialty .md file before Agent 2 runs."""
    if not user_input or not user_input.strip():
        return
    filename = discipline.lower().replace(" ", "_").replace("/", "_") + ".md"
    path = _ROLES_DIR / filename

    if path.exists():
        existing = path.read_text().strip()
        prompt = _MERGE_PROMPT.format(existing=existing, user_input=user_input.strip())
    else:
        prompt = _MERGE_PROMPT_NEW.format(discipline=discipline, user_input=user_input.strip())

    print(f"  [Merge] Updating specialty guidance for {discipline}...")
    resp = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    updated = resp.choices[0].message.content.strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated + "\n")
    print(f"  [Merge] Saved updated guidance → {path.name}")


_ROLES_PROMPT = """\
{discipline_context}Search the web for the professional roles and service activities of {name} at {school}.{profile_hint}

Also find their current academic rank/title (e.g. "Professor", "Associate Professor", \
"Assistant Professor", "Instructor", "Lecturer"). Look at their institutional faculty \
profile page. Return null if not clearly stated.

Include ONLY roles that involve active service or responsibility beyond their faculty appointment:
- Editorial board positions (e.g. "Associate Editor, Diabetes Care")
- Professional society committee roles (e.g. "President, Pediatric Endocrine Society")
- Institutional or divisional leadership (e.g. "Division Chief", "Program Director", "Center Director")
- Training/education program roles (e.g. "Fellowship Program Director", "Clerkship Director")

CRITICAL — attribution. Only include a role if the source text explicitly attributes it to \
{name} specifically. On team, faculty-directory, or division listing pages, words like \
"Director", "Faculty", "Chief", or "Leadership" are often SECTION HEADINGS, filter labels, or \
column categories that group many people — they are NOT roles held by every person listed under \
them. Never turn such a heading/label into a role unless the text clearly ties it to {name} by \
name (e.g. "{name}, Director of ..."). Roles found on this person's OWN individual profile/bio \
page are valid even if their name is not repeated in each bullet, because the whole page is about \
them. When in doubt, leave the role out.

Do NOT include:
- Academic titles or ranks (Professor, Assistant Professor, Instructor, Lecturer, Attending)
- General employment or affiliation (e.g. "Faculty, Stanford")
- Degrees or credentials (MD, PhD, etc.)
- Roles explicitly described as "former", "past", "previously", "emeritus", or "ex-"
- Roles with end dates clearly in the past (e.g. "(2011 - 2013)") — only include current/active roles

Respond ONLY with a JSON object with exactly two keys:
- "title": string or null — their current academic rank as it appears on their profile
- "roles": array of role objects

Each role item must have exactly these keys:
- "category": one of "editorial", "society", "leadership", "training"
- "role": the specific role title (e.g. "Associate Editor")
- "org": the organization or journal name (e.g. "Diabetes Care")
- "source_url": URL of the webpage where you found this
- "evidence_text": a RAW VERBATIM EXCERPT (up to 800 characters) copied word-for-word from the surrounding section of the source page where this role appears. Copy the full surrounding paragraph, list block, or table section — do NOT cherry-pick only the confirming sentence. Include enough context that a separate reviewer reading only this excerpt could independently confirm or deny the role claim. Do NOT paraphrase or summarize. If you cannot find raw source text, leave this field as an empty string.
- "retrieved_at": current UTC timestamp in ISO 8601 format

If no qualifying roles are found, return {{"title": null, "roles": []}}.
No markdown fences, no explanation — only the JSON object.
"""

_TITLE_PROMPT = """\
Look up the faculty profile page for {name} at {school}.{profile_hint}
Return ONLY a JSON object with three keys:
- "title": their current academic rank exactly as listed on their institutional profile \
(e.g. "Professor", "Associate Professor", "Assistant Professor", "Clinical Professor", \
"Instructor", "Lecturer"). Return null if not clearly stated.
- "profile_urls": a list of direct URLs for this person's individual faculty profile pages. \
Include both the department/division page (e.g. "https://pediatrics.ucsf.edu/people/jane-smith") \
AND the university-wide profiles page (e.g. "https://profiles.ucsf.edu/jane.smith") if both exist. \
List the department page first. Return [] if none found.

No markdown fences, no explanation — only the JSON object.
"""

def _clean_name_parts(full_name: str) -> list[str]:
    # Use the shared comprehensive credential set (_CREDENTIALS, defined below) and an
    # uppercase compare so the NIH name parse strips the same degrees/suffixes as the
    # Scopus/h-index parse. Otherwise e.g. "Anna Gloyn, DPhil" parses a last name of
    # "DPhil" (and "John Smith Jr" a last name of "Jr") and finds no NIH grants.
    return [p.strip(".,") for p in full_name.split()
            if p.strip(".,") and p.strip(".,").upper() not in _CREDENTIALS]


def _complete_objects(s: str) -> list[dict]:
    """Return all complete JSON objects from a string that begins with a (possibly
    truncated) array. Stops at the first incomplete object instead of raising."""
    objs: list[dict] = []
    i = s.find("[")
    if i == -1:
        return objs
    i += 1
    n = len(s)
    while i < n:
        while i < n and s[i] in " \t\r\n,":
            i += 1
        if i >= n or s[i] == "]":
            break
        if s[i] != "{":
            break
        depth, j, in_str, esc, end = 0, i, False, False, -1
        while j < n:
            c = s[j]
            if in_str:
                if esc:        esc = False
                elif c == "\\": esc = True
                elif c == '"':  in_str = False
            elif c == '"':      in_str = True
            elif c == "{":      depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
            j += 1
        if end == -1:
            break  # truncated object — stop here
        try:
            objs.append(json.loads(s[i:end + 1]))
        except json.JSONDecodeError:
            break
        i = end + 1
    return objs


def _parse_json(raw: str) -> dict:
    clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        # Truncated response (e.g. a faculty member with many roles overflows the output
        # limit mid-array). Salvage the title plus every complete role object.
        m = re.search(r'"title"\s*:\s*(null|"(?:[^"\\]|\\.)*")', clean)
        title = None
        if m and m.group(1) != "null":
            try:
                title = json.loads(m.group(1))
            except json.JSONDecodeError:
                title = None
        roles_at = clean.find('"roles"')
        roles = _complete_objects(clean[roles_at:]) if roles_at != -1 else []
        print(f"      [Roles] Recovered {len(roles)} role(s) from truncated JSON")
        return {"title": title, "roles": roles}


import functools
import os

_NIH_SEARCH_URL = "https://api.reporter.nih.gov/v2/projects/search"
_NIH_FIELDS = ["project_num", "project_title", "fiscal_year", "organization", "principal_investigators"]
_NIH_CONFIDENCE_THRESHOLD = 0.60   # Phase 2 when institution is confirmed (inst_score=1.0)
_NIH_STRICT_THRESHOLD     = 0.78   # Phase 2 when institution is unknown (inst_score=0.5)

# Scoring weights — must sum to 1.0
# name = who we're looking for (primary)
# inst = where they work — checked against xlsx aliases (people move, so not a hard reject)
# dept = NIH dept_type field — present for university grants, null for hospital grants
_W_NAME = 0.50
_W_INST = 0.35
_W_DEPT = 0.15


def _score_dept(org_dept: str, discipline: str) -> float:
    """0.5 if no data or doesn't match; 1.0 if dept confirms discipline."""
    if not org_dept:
        return 0.5
    dept  = org_dept.lower()
    words = [w for w in discipline.lower().split() if len(w) > 3]
    if any(w in dept for w in words):
        return 1.0
    return 0.5  # present but unrelated — ignore rather than penalise


def _inst_matches(org: str, aliases) -> bool:
    """True if org name matches any alias via substring or fuzzy (≥85)."""
    org_l = org.lower()
    for alias in aliases:
        if alias in org_l or org_l in alias:
            return True
        if len(alias) >= 8 and fuzz.partial_ratio(org_l, alias) >= 85:
            return True
    return False


def _score_inst(orgs: set[str], target_aliases: tuple[str, ...], all_known: dict) -> float:
    """1.0 confirmed at target; 0.3 at a different known school; 0.5 unknown."""
    if not orgs:
        return 0.5
    for org in orgs:
        if _inst_matches(org, target_aliases):
            return 1.0
    for org in orgs:
        for aliases in all_known.values():
            if _inst_matches(org, aliases):
                return 0.3
    return 0.5


def _nih_org_names_for(school: str) -> list[str]:
    """
    Build org_names list for the NIH Phase 1 API query using xlsx aliases.
    Strips med-school suffixes so names match NIH's abbreviated org_name format.
    """
    aliases = _xlsx_aliases_for(school)
    if not aliases:
        parts = [p.strip() for p in school.split(" / ")]
        aliases = tuple(p.lower() for p in parts if p)

    result: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        a = alias.strip()
        if not a or a in seen:
            continue
        seen.add(a)
        result.append(a)
        # Also add version with med-school suffix stripped (NIH stores "Stanford University", not the full name)
        for suffix in _MED_SUFFIXES:
            if a.endswith(suffix):
                stripped = a[: len(a) - len(suffix)].strip()
                if stripped and stripped not in seen:
                    seen.add(stripped)
                    result.append(stripped)
                break
    return result


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
    def __init__(self, discipline: str = ""):
        self._discipline = discipline

    # ── helpers ──────────────────────────────────────────────────────────────

    def _parse_results_into_profiles(
        self, results: list[dict], first_name: str, last_name: str
    ) -> dict[int, dict]:
        """Aggregate NIH search results into per-profile-id dicts."""
        profiles: dict[int, dict] = {}
        for item in results:
            org      = item.get("organization") or {}
            org_name = (org.get("org_name") or "").lower()
            org_dept = (org.get("dept_type") or "").lower()
            for pi in (item.get("principal_investigators") or []):
                pid = pi.get("profile_id")
                if not pid:
                    continue
                pi_first = (pi.get("first_name") or "").strip().lower()
                pi_last  = (pi.get("last_name")  or "").strip().lower()
                first_sim  = fuzz.ratio(first_name.lower(), pi_first) / 100.0
                last_sim   = fuzz.ratio(last_name.lower(),  pi_last)  / 100.0
                name_score = 0.5 * first_sim + 0.5 * last_sim
                if name_score < 0.5:
                    continue
                if pid not in profiles:
                    profiles[pid] = {
                        "pi_name":    f"{pi.get('first_name','')} {pi.get('last_name','')}".strip(),
                        "name_score": name_score,
                        "orgs":       set(),
                        "depts":      set(),
                    }
                profiles[pid]["orgs"].add(org_name)
                if org_dept:
                    profiles[pid]["depts"].add(org_dept)
        return profiles

    def _grants_for_profile(
        self, profile_id: int, fallback_results: list[dict], unconfirmed: bool = False
    ) -> list[Grant]:
        """Fetch all grants for a confirmed profile_id and return them."""
        try:
            all_items = _nih_fetch_all_by_profile(profile_id)
        except Exception as e:
            print(f"      [NIH] Profile pull failed for profile_id={profile_id}: {e}")
            all_items = fallback_results
        seen: set[str] = set()
        grants: list[Grant] = []
        for item in all_items:
            num = item.get("project_num", "Unknown")
            if num not in seen:
                seen.add(num)
                g: Grant = {"project_num": num, "title": item.get("project_title", "")}
                if unconfirmed:
                    g["institution_unconfirmed"] = True
                grants.append(g)
        return grants

    # ── main entry point ─────────────────────────────────────────────────────

    def get_grants(self, name: str, school: str) -> list[Grant]:
        """
        Two-phase NIH identity matching:

        Phase 1 — targeted (high confidence):
          Search pi_names + org_names using xlsx institution aliases (hospital names +
          university short forms). If any result comes back the person has grants at
          this institution → take the best name match, pull ALL grants by profile_id.

        Phase 2 — broad fallback (scored, flagged as unconfirmed):
          No institution match found (person may have moved or hold no grants here).
          Search by name only, score candidates on name (0.50) + inst (0.35) + dept (0.15).
          dept_type is present for university grants, null for hospital grants — treated
          as neutral when absent. Returns nothing if best candidate is below threshold.
        """
        parts = _clean_name_parts(name)
        if len(parts) < 2:
            return []
        first_name, last_name = parts[0], parts[-1]

        org_names_query  = _nih_org_names_for(school)
        target_aliases   = _xlsx_aliases_for(school) or (school.lower(),)
        all_known        = _load_xlsx_aliases()

        # ── Phase 1: name + institution ───────────────────────────────────────
        print(f"      [NIH] Phase 1: searching '{first_name} {last_name}' at {school}")
        try:
            p1_results = _nih_search({
                "criteria": {
                    "pi_names":  [{"first_name": first_name, "last_name": last_name}],
                    "org_names": org_names_query,
                },
                "fields": _NIH_FIELDS,
                "limit": 500,
            })
        except Exception as e:
            print(f"      [NIH] Phase 1 search failed: {e}")
            p1_results = []

        if p1_results:
            profiles = self._parse_results_into_profiles(p1_results, first_name, last_name)
            if profiles:
                best_pid = max(profiles, key=lambda pid: profiles[pid]["name_score"])
                best     = profiles[best_pid]
                print(f"      [NIH] Phase 1 match: '{best['pi_name']}' "
                      f"(name={best['name_score']:.2f}, profile_id={best_pid})")
                grants = self._grants_for_profile(best_pid, p1_results, unconfirmed=False)
                print(f"      [NIH] {name}: {len(grants)} grant(s) — institution confirmed")
                return grants

        print(f"      [NIH] Phase 1: no grants found at {school} — trying broad search")

        # ── Phase 2: name only + scoring ──────────────────────────────────────
        try:
            p2_results = _nih_search({
                "criteria": {"pi_names": [{"first_name": first_name, "last_name": last_name}]},
                "fields": _NIH_FIELDS,
                "limit": 500,
            })
        except Exception as e:
            print(f"      [NIH] Phase 2 search failed: {e}")
            return []

        if not p2_results:
            print(f"      [NIH] No NIH grants found for {name}")
            return []

        print(f"      [NIH] Phase 2: {len(p2_results)} candidate grant(s) across all institutions")
        profiles = self._parse_results_into_profiles(p2_results, first_name, last_name)
        if not profiles:
            return []

        for prof in profiles.values():
            prof["dept_score"] = max(
                (_score_dept(d, self._discipline) for d in prof["depts"]),
                default=0.5,
            )
            prof["inst_score"] = _score_inst(prof["orgs"], target_aliases, all_known)

        def _combined(p: dict) -> float:
            return _W_NAME * p["name_score"] + _W_INST * p["inst_score"] + _W_DEPT * p["dept_score"]

        ranked        = sorted(profiles.items(), key=lambda kv: _combined(kv[1]), reverse=True)
        best_pid, best = ranked[0]
        score         = _combined(best)
        ambiguous     = len(ranked) > 1 and (score - _combined(ranked[1][1])) < 0.05

        print(f"      [NIH] Phase 2 best: '{best['pi_name']}' "
              f"(score={score:.2f} | name={best['name_score']:.2f} "
              f"inst={best['inst_score']:.2f} dept={best['dept_score']:.2f} "
              f"| profiles={len(profiles)}"
              f"{' | AMBIGUOUS' if ambiguous else ''})")

        # Confirmed at a different known institution — definitely the wrong person
        if best["inst_score"] == 0.3:
            print(f"      [NIH] Phase 2: '{best['pi_name']}' confirmed at a different institution — skipping {name}")
            return []

        # Multiple close profiles with unconfirmed institution — too ambiguous to trust
        if ambiguous and best["inst_score"] < 1.0:
            print(f"      [NIH] Phase 2: ambiguous match (score gap < 0.05) with unconfirmed institution — skipping {name}")
            return []

        # Apply stricter threshold when institution is not confirmed
        threshold = _NIH_CONFIDENCE_THRESHOLD if best["inst_score"] >= 1.0 else _NIH_STRICT_THRESHOLD
        if score < threshold:
            print(f"      [NIH] Low confidence (score={score:.2f} < {threshold:.2f}) — skipping {name}")
            return []

        grants = self._grants_for_profile(best_pid, p2_results, unconfirmed=True)
        print(f"      [NIH] {name}: {len(grants)} grant(s) — institution unconfirmed")
        return grants


class LiveRolesProvider(RolesProvider):
    def __init__(self, openai_client, discipline: str = "", considerations: str = ""):
        self._client = openai_client
        self._discipline_context = _load_discipline_context(discipline) if discipline else ""
        if considerations:
            self._discipline_context += f"\nAdditional guidance for this run:\n{considerations}\n"

    def _fetch(self, name: str, school: str, profile_url: str = "") -> dict:
        """Make one web search call and return the parsed JSON (title + roles)."""
        profile_hint = f"\nStart by checking their profile page: {profile_url}" if profile_url else ""
        prompt = _ROLES_PROMPT.format(
            name=name, school=school,
            discipline_context=self._discipline_context,
            profile_hint=profile_hint,
        )
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
        return _parse_json(raw)

    def _process_roles(self, parsed: dict) -> list[RoleEvidence]:
        roles = parsed.get("roles", [])
        now = datetime.now(timezone.utc).isoformat()
        for role in roles:
            role.setdefault("retrieved_at", now)
            role.setdefault("model", _LIVE_MODEL)
            if role.get("source_url"):
                role["source_url"] = clean_url(role["source_url"])
        return roles

    def get_roles(self, name: str, school: str, profile_url: str = "") -> list[RoleEvidence]:
        return self._process_roles(self._fetch(name, school, profile_url))

    def get_roles_and_title(self, name: str, school: str, profile_url: str = "") -> tuple[str | None, list[RoleEvidence], list[str]]:
        """Two web search calls: one for roles, one dedicated title lookup.
        Returns (title, roles, discovered_profile_urls)."""
        roles = self._process_roles(self._fetch(name, school, profile_url))
        profile_hint = f"\nTheir profile page is: {profile_url}" if profile_url else ""
        prompt = _TITLE_PROMPT.format(name=name, school=school, profile_hint=profile_hint)
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
        try:
            parsed = _parse_json(raw)
            title = parsed.get("title") or None
            urls = parsed.get("profile_urls") or []
            discovered_urls = [c for u in urls if isinstance(u, str) and (c := clean_url(u))]
        except Exception:
            title = None
            discovered_urls = []
        return title, roles, discovered_urls


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


_ALT_FIRST_RE = re.compile(r'[(""]([^)""]+)[)""]')


def _extract_alternate_first(name: str) -> str | None:
    """Extract a parenthetical or quoted preferred/alternate first name.

    'Cecilio (Leo) Lopez'    → 'Leo'
    'Azadeh "Azy" Issapouru' → 'Azy'
    'Brian Han'              → None
    """
    m = _ALT_FIRST_RE.search(name)
    return m.group(1).strip() if m else None



def _affil_matches(affil_str: str, aliases: list[str]) -> bool:
    """True if an alias's distinctive words all appear in the affiliation string.

    "Distinctive" = drops generic words (university, medical, hospital, …) and
    1-2 char tokens. An alias matches only when every one of its distinctive
    words is present in the affiliation, so "johns hopkins" matches
    "Johns Hopkins University" but "johns hopkins bayview" does not match a
    plain "Johns Hopkins University" affiliation. No fuzzy threshold.
    """
    if not affil_str:
        return False
    affil_words = set(re.findall(r"[a-zA-Z]+", affil_str.lower()))
    for alias in aliases:
        d_words = [w for w in re.findall(r"[a-zA-Z]+", alias.lower())
                   if w not in _GENERIC_WORDS and len(w) > 2]
        if d_words and all(w in affil_words for w in d_words):
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


_OA_SEARCH_URL = "https://api.openalex.org/authors"
_OA_EMAIL = "ericmarkar18@gmail.com"
# Semaphore: 8 concurrent when API key present (100 req/sec tier), else 3 (polite pool 10 req/sec)
_OA_API_KEY: str | None = None  # set at module load from env


def _oa_init():
    global _OA_API_KEY, _OA_SEMAPHORE
    import os
    _OA_API_KEY = os.environ.get("OPENALEX_API_KEY") or None
    limit = 8 if _OA_API_KEY else 3
    _OA_SEMAPHORE = threading.Semaphore(limit)
    if _OA_API_KEY:
        print(f"[OpenAlex] API key loaded — using authenticated pool (concurrency={limit})")

_OA_SEMAPHORE = threading.Semaphore(3)
_oa_init()


def _openalex_search(first: str, last: str) -> list[dict]:
    """Search OpenAlex authors by name. Returns up to 10 results."""
    for attempt in range(3):
        try:
            params: dict = {
                "search": f"{first} {last}",
                "per-page": 10,
                "mailto": _OA_EMAIL,
            }
            headers: dict = {"Accept": "application/json"}
            if _OA_API_KEY:
                headers["Authorization"] = f"Bearer {_OA_API_KEY}"
            with _OA_SEMAPHORE:
                resp = requests.get(
                    _OA_SEARCH_URL,
                    params=params,
                    headers=headers,
                    timeout=15,
                )
            if resp.status_code == 429:
                # x-ratelimit-reset is a Unix timestamp, not a duration
                reset_ts = resp.headers.get("x-ratelimit-reset")
                retry_after = resp.headers.get("Retry-After")
                if reset_ts and str(reset_ts).isdigit():
                    wait = max(float(reset_ts) - time.time(), 1.0)
                elif retry_after and str(retry_after).isdigit():
                    wait = float(retry_after)
                else:
                    wait = 5.0
                wait = min(wait, 15.0)  # never block more than 15s
                print(f"      [OpenAlex] Rate limited — waiting {wait:.1f}s (attempt {attempt+1}/3)")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json().get("results", [])
        except Exception:
            if attempt < 2:
                time.sleep(2 ** attempt)
    return []


def _oa_affil_str(entry: dict) -> str:
    """Extract affiliation string from OpenAlex author entry."""
    institutions = entry.get("last_known_institutions") or []
    return " ".join(inst.get("display_name", "") for inst in institutions)


def _oa_name_matches(first: str, last: str, entry: dict) -> bool:
    """True if OpenAlex entry display_name matches (first, last)."""
    display = entry.get("display_name", "")
    parts = display.split()
    if not parts:
        return False
    oa_last = parts[-1].lower().strip(".,")
    oa_first = parts[0].lower().strip(".,")
    if fuzz.ratio(last.lower(), oa_last) < 90:
        return False
    if not first or not oa_first:
        return True
    return oa_first[0] == first[0].lower()


# ── Scopus (primary h-index source) ───────────────────────────────────────────

_SCOPUS_SEARCH_URL = "https://api.elsevier.com/content/search/author"
_SCOPUS_AUTHOR_URL = "https://api.elsevier.com/content/author/author_id/{}"
_SCOPUS_FIELDS = "dc:identifier,preferred-name,affiliation-current,document-count,subject-area"


class _ScopusUnavailable(Exception):
    """Raised when Scopus can't answer (quota, rate-limit, HTTP error) → fall back to OpenAlex."""


def _affil_score(affil_str: str, aliases: list[str]) -> float:
    """Graded affiliation match for ranking (no threshold).

    1.0 — every distinctive word of some alias is present in the affiliation
    0.5 — some (but not all) distinctive words of an alias are present
    0.0 — no distinctive word matches
    "Distinctive" drops generic words (university, medical, hospital, …) and ≤2-char tokens.
    """
    if not affil_str:
        return 0.0
    affil_words = set(re.findall(r"[a-zA-Z]+", affil_str.lower()))
    best = 0.0
    for alias in aliases:
        d_words = [w for w in re.findall(r"[a-zA-Z]+", alias.lower())
                   if w not in _GENERIC_WORDS and len(w) > 2]
        if not d_words:
            continue
        hits = sum(w in affil_words for w in d_words)
        if hits == len(d_words):
            return 1.0
        if hits > 0:
            best = 0.5
    return best


def _scopus_name_score(first: str, last: str, pref: dict) -> int:
    """2 = exact first name (ignoring middle), 1 = first-initial match, 0 = no match."""
    s_first = (pref.get("given-name") or "").lower().strip()
    s_last = (pref.get("surname") or "").lower().strip()
    if fuzz.ratio(last.lower(), s_last) < 90:
        return 0
    if not first or not s_first:
        return 1
    if s_first[0] != first[0].lower():
        return 0
    return 2 if s_first.split()[0].strip(".,") == first.lower() else 1


def _scopus_subject_score(entry: dict, discipline: str) -> int:
    """1 if any Scopus subject-area label overlaps the discipline words, else 0."""
    if not discipline:
        return 0
    sas = entry.get("subject-area", [])
    if isinstance(sas, dict):
        sas = [sas]
    words = [w for w in discipline.lower().split() if len(w) > 3]
    txt = " ".join((s.get("$") or "").lower() for s in sas if isinstance(s, dict))
    return 1 if any(w in txt for w in words) else 0


def _scopus_affil_str(entry: dict) -> str:
    a = entry.get("affiliation-current", {})
    if isinstance(a, list):
        return " ".join(x.get("affiliation-name", "") for x in a if isinstance(x, dict))
    if isinstance(a, dict):
        return a.get("affiliation-name", "")
    return ""


class LiveHIndexProvider(HIndexProvider):
    """H-index lookup: Scopus first (curated disambiguation), OpenAlex as fallback.

    Scopus is tried first. It falls back to OpenAlex when Scopus is unavailable
    (no key, quota/rate-limit, HTTP error) or inconclusive (person not found at
    this institution under this name).
    """

    def __init__(self, scopus_api_key: str | None = None, discipline: str = ""):
        self._api_key = scopus_api_key or os.environ.get("SCOPUS_API_KEY")
        self._insttoken = os.environ.get("SCOPUS_INSTTOKEN")
        self._discipline = discipline

    def get_hindex(self, name: str, school: str) -> HIndexEvidence:
        now = datetime.now(timezone.utc).isoformat()

        # ── Primary: Scopus ──────────────────────────────────────────────────
        if self._api_key:
            try:
                result = self._scopus_lookup(name, school, now)
                if result is not None:
                    return result
                print(f"      [HIndex] Scopus inconclusive for {name} — trying OpenAlex")
            except _ScopusUnavailable as e:
                print(f"      [HIndex] Scopus unavailable ({e}) — falling back to OpenAlex")
            except Exception as e:
                print(f"      [HIndex] Scopus error ({e}) — falling back to OpenAlex")
        else:
            print(f"      [HIndex] No Scopus key set — using OpenAlex")

        # ── Fallback: OpenAlex ───────────────────────────────────────────────
        try:
            return self._get_hindex_inner(name, school, now)
        except Exception as exc:
            print(f"      [OpenAlex] Unrecoverable error for {name}: {exc}")
            return {
                "h_index": None, "scopus_author_id": None,
                "source_url": "", "evidence_text": f"OpenAlex error: {exc}",
                "retrieved_at": now,
            }

    # ── Scopus implementation ─────────────────────────────────────────────────

    def _scopus_headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self._insttoken:
            headers["X-ELS-Insttoken"] = self._insttoken
        return headers

    def _scopus_search(self, first: str, last: str) -> list[dict]:
        resp = requests.get(
            _SCOPUS_SEARCH_URL,
            params={
                "query": f"AUTHLASTNAME({last}) AND AUTHFIRST({first})",
                "field": _SCOPUS_FIELDS,
                "count": 25,
                "apiKey": self._api_key,
            },
            headers=self._scopus_headers(),
            timeout=15,
        )
        if resp.status_code == 429:
            raise _ScopusUnavailable("rate limited / quota exhausted")
        if resp.status_code != 200:
            raise _ScopusUnavailable(f"author search HTTP {resp.status_code}")
        entries = resp.json().get("search-results", {}).get("entry", [])
        if entries and isinstance(entries[0], dict) and "error" in entries[0]:
            return []
        return entries

    def _scopus_hindex(self, author_id: str) -> int | None:
        resp = requests.get(
            _SCOPUS_AUTHOR_URL.format(author_id),
            params={"field": "h-index", "apiKey": self._api_key},
            headers=self._scopus_headers(),
            timeout=15,
        )
        if resp.status_code == 429:
            raise _ScopusUnavailable("rate limited / quota exhausted")
        if resp.status_code != 200:
            raise _ScopusUnavailable(f"author profile HTTP {resp.status_code}")
        d = resp.json().get("author-retrieval-response", [{}])[0]
        h = d.get("h-index")
        return int(h) if h is not None else None

    def _scopus_lookup(self, name: str, school: str, now: str) -> HIndexEvidence | None:
        """Return HIndexEvidence if confidently found in Scopus, else None (→ OpenAlex).

        Ranking (no document-count): name_score + affil_score, then subject as a
        tiebreak. The winner must have an affiliation signal (affil_score > 0).
        When 2+ candidates tie on name+affil+subject (fragmentation), pick the one
        with the highest h-index — for a person split across profiles, the real
        profile carries the real h-index.
        """
        first, _middle, last = _parse_name_for_scopus(name)
        if not first or not last:
            return None

        aliases = list(_xlsx_aliases_for(school) or [school.lower()])

        entries = self._scopus_search(first, last)
        if not entries:
            alt = _extract_alternate_first(name)
            if alt:
                entries = self._scopus_search(alt, last)
        if not entries:
            return None

        scored: list[dict] = []
        for e in entries:
            ns = _scopus_name_score(first, last, e.get("preferred-name", {}))
            if ns == 0:
                continue
            affil = _scopus_affil_str(e)
            scored.append({
                "rank": ns + _affil_score(affil, aliases),
                "name_score": ns,
                "affil_score": _affil_score(affil, aliases),
                "subject": _scopus_subject_score(e, self._discipline),
                "entry": e,
                "affil": affil,
            })
        if not scored:
            return None  # name didn't match anyone — let OpenAlex try

        scored.sort(key=lambda c: (c["rank"], c["subject"]), reverse=True)
        top = scored[0]

        # No candidate is actually at this institution → not confidently found.
        if top["affil_score"] == 0:
            return None

        # Candidates tied with the winner on name+affil+subject (fragmentation).
        tied = [c for c in scored
                if c["rank"] == top["rank"] and c["subject"] == top["subject"]]

        def _aid(c: dict) -> str:
            return c["entry"].get("dc:identifier", "").replace("AUTHOR_ID:", "")

        if len(tied) > 1:
            # h-index tiebreak: the real profile of a fragmented person holds the real h-index.
            best_c, best_h = None, -1
            for c in tied:
                aid = _aid(c)
                if not aid:
                    continue
                h = self._scopus_hindex(aid)
                if h is not None and h > best_h:
                    best_c, best_h = c, h
            if best_c is None:
                best_c, h_index = tied[0], None
            else:
                top, h_index = best_c, best_h
        else:
            aid = _aid(top)
            h_index = self._scopus_hindex(aid) if aid else None

        confidence = (
            "high" if top["name_score"] == 2 and top["affil_score"] == 1.0
            else "medium" if top["affil_score"] >= 0.5
            else "needs_review"
        )

        author_id = _aid(top)
        pref = top["entry"].get("preferred-name", {})
        display = f"{pref.get('given-name', '')} {pref.get('surname', '')}".strip()
        print(f"      [Scopus] Matched '{display}' @ '{top['affil']}' "
              f"(confidence={confidence}, h-index={h_index})")

        return {
            "h_index": h_index,
            "scopus_author_id": author_id,
            "source_url": f"https://www.scopus.com/authid/detail.uri?authorId={author_id}",
            "evidence_text": (
                f"Scopus: h-index={h_index}, affil='{top['affil']}', "
                f"confidence={confidence}"
            ),
            "retrieved_at": now,
        }

    def _oa_find_candidate(
        self, first: str, last: str, aliases: list[str], middle: str = ""
    ) -> tuple[dict | None, str, str]:
        """Search OpenAlex for (first, last) and return the best matching author.

        Returns (entry | None, confidence, reason):
          "high"         — affiliation confirmed
          "needs_review" — single name match, affiliation not verified
          "failed"       — no usable match
        """
        print(f"      [OpenAlex] Searching: {first} {last}")
        entries = _openalex_search(first, last)
        if not entries:
            return None, "failed", f"No OpenAlex results for '{first} {last}'"

        # Filter: name must match AND author must have at least one publication
        name_matches = [
            e for e in entries
            if _oa_name_matches(first, last, e) and (e.get("works_count") or 0) > 0
        ]
        if not name_matches:
            return None, "failed", f"No name match in OpenAlex for '{first} {last}'"

        affil_match = next(
            (e for e in name_matches if _affil_matches(_oa_affil_str(e), aliases)),
            None,
        )
        if affil_match:
            h = (affil_match.get("summary_stats") or {}).get("h_index")
            w = affil_match.get("works_count", "?")
            print(f"      [OpenAlex] Affiliation match: '{affil_match.get('display_name','')}' h={h} works={w}")
            return affil_match, "high", ""

        if len(name_matches) == 1:
            e = name_matches[0]
            h = (e.get("summary_stats") or {}).get("h_index")
            w = e.get("works_count", "?")
            display = e.get("display_name", "")
            affil = _oa_affil_str(e)
            print(f"      [OpenAlex] Single match '{display}' @ '{affil}' h={h} works={w} — no affil confirmed")
            return e, "needs_review", ""

        display = name_matches[0].get("display_name", "")
        print(f"      [OpenAlex] Multiple matches for '{last}', no affiliation confirmed. Top: '{display}'")
        return None, "failed", f"Multiple name matches for '{last}', no affiliation confirmed"

    def _get_hindex_inner(self, name: str, school: str, now: str) -> HIndexEvidence:
        first, middle, last = _parse_name_for_scopus(name)
        if not first or not last:
            return {
                "h_index": None, "scopus_author_id": None,
                "source_url": "", "evidence_text": f"Could not parse name: {name}",
                "retrieved_at": now,
            }

        xlsx_aliases = _xlsx_aliases_for(school)
        aliases = list(xlsx_aliases) if xlsx_aliases else [school.lower()]

        best_entry, confidence, reason = self._oa_find_candidate(first, last, aliases, middle)

        # Alternate-name fallback: retry with parenthetical/quoted preferred name
        if confidence == "failed":
            alternate = _extract_alternate_first(name)
            if alternate:
                print(
                    f"      [OpenAlex] Initial match for '{name}' failed affiliation gate; "
                    f"retrying with alternate first name '{alternate}'."
                )
                best_entry, confidence, alt_reason = self._oa_find_candidate(alternate, last, aliases)
                if confidence == "failed":
                    return {
                        "h_index": None, "scopus_author_id": None,
                        "source_url": "",
                        "evidence_text": (
                            f"Alternate first name '{alternate}' also failed affiliation gate "
                            f"for {name}: {alt_reason}"
                        ),
                        "qc_hindex_confidence": "yellow",
                        "retrieved_at": now,
                    }
            else:
                return {
                    "h_index": None, "scopus_author_id": None,
                    "source_url": "", "evidence_text": reason,
                    "retrieved_at": now,
                }

        oa_id = best_entry.get("id", "")
        display_name = best_entry.get("display_name", "")
        display_affil = _oa_affil_str(best_entry)
        stats = best_entry.get("summary_stats") or {}
        h_index = stats.get("h_index")
        works_count = best_entry.get("works_count", "?")

        print(f"      [OpenAlex] Matched '{display_name}' @ '{display_affil}' (confidence={confidence}, h-index={h_index})")

        return {
            "h_index": h_index,
            "scopus_author_id": None,
            "source_url": oa_id,
            "evidence_text": (
                f"OpenAlex: h-index={h_index}, works={works_count}, "
                f"affil='{display_affil}', confidence={confidence}"
            ),
            "retrieved_at": now,
        }
