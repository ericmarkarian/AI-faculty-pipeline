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


class LiveNihProvider(NihProvider):
    def get_grants(self, name: str, school: str) -> list[Grant]:
        parts = _clean_name_parts(name)
        if len(parts) < 2:
            return []

        first_name, last_name = parts[0], parts[-1]
        org_names = _INSTITUTION_NIH_NAMES.get(school, [school.upper().split("/")[0].strip()])

        payload = {
            "criteria": {
                "pi_names": [{"first_name": first_name, "last_name": last_name}],
                "org_names": org_names,
            },
            "fields": ["project_num", "project_title", "fiscal_year"],
            "limit": 100,
            "offset": 0,
        }
        r = requests.post(
            "https://api.reporter.nih.gov/v2/projects/search",
            json=payload,
            timeout=15,
        )
        r.raise_for_status()

        seen: set[str] = set()
        grants: list[Grant] = []
        for item in r.json().get("results", []):
            num = item.get("project_num", "Unknown")
            if num not in seen:
                seen.add(num)
                grants.append({"project_num": num, "title": item.get("project_title", "")})
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

ACCEPT_THRESHOLD = 0.6
REVIEW_THRESHOLD = 0.35

_GENERIC_WORDS = {
    "university", "universities", "medical", "center", "school", "hospital",
    "health", "institute", "system", "care", "college", "of", "and", "the",
    "at", "for", "children", "childrens", "general", "national", "research",
    "science", "sciences", "medicine", "clinic", "foundation", "department",
    "child", "maternal", "cancer", "stem", "cell", "biology", "regenerative"
}

_DISCIPLINE_KEYWORDS = {
    "Pediatric Endocrinology": [
        "endocrinology", "pediatrics", "pediatric", "diabetes",
        "metabolism", "endocrine", "growth", "thyroid", "adrenal"
    ]
}

def _distinctive_words(name: str) -> list[str]:
    words = re.findall(r"[a-zA-Z]+", name.lower())
    return [w for w in words if w not in _GENERIC_WORDS and len(w) > 2]

def _affiliation_score(affil_string: str, aliases: list[str], city: str = "") -> float:
    if not affil_string:
        return 0.0
    affil = affil_string.lower()
    affil_words = set(re.findall(r"[a-zA-Z]+", affil))
    score = 0.0
    for alias in aliases:
        d_words = _distinctive_words(alias)
        if not d_words:
            continue
        if not any(w in affil_words for w in d_words):
            continue
        if fuzz.partial_ratio(alias.lower(), affil) >= 85:
            score += 0.5
            break
    if city and city.lower() in affil:
        score += 0.2
    return min(score, 1.0)

def _subject_area_score(subject_areas: list[str], discipline: str) -> float:
    if not subject_areas:
        return 0.0
    keywords = _DISCIPLINE_KEYWORDS.get(discipline, [discipline.lower()])
    areas_lower = " ".join(subject_areas).lower()
    matched = sum(1 for kw in keywords if kw in areas_lower)
    return min(matched * 0.1, 0.3)

def _clean_name_for_scopus(full_name: str) -> tuple[str, str]:
    """Better name cleaner — handles comma-separated credentials like 'Anna Gloyn, DPhil'."""
    credentials = {
        "MD", "PHD", "DO", "DDS", "DMD", "DVM", "PHARMD", "DRPH",
        "DPHIL", "MBBS", "MPH", "MS", "MSC", "MSN", "MHS", "MHA", "MBA",
        "FAAP", "FACS", "FACP", "FACE", "DFAACAP", "RN", "NP", "PA",
        "JR", "SR", "II", "III", "IV",
    }
    name = full_name.split(",")[0].strip()
    parts = [p for p in name.split() if p.strip(".,").upper() not in credentials]
    parts = [p for p in parts if p.strip(".,")]
    if len(parts) < 2:
        return "", ""
    return parts[0], parts[-1]


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

        first_name, last_name = _clean_name_for_scopus(name)
        if not first_name or not last_name:
            return {
                "h_index": None, "scopus_author_id": None,
                "source_url": "", "evidence_text": f"Could not parse name: {name}",
                "retrieved_at": now,
            }

        # Step 1: Broad search — no affiliation filter, get 25 candidates
        print(f"      [Scopus] Querying: AUTHLASTNAME({last_name}) AND AUTHFIRST({first_name})")
        search_resp = requests.get(
            "https://api.elsevier.com/content/search/author",
            params={
                "query": f"AUTHLASTNAME({last_name}) AND AUTHFIRST({first_name})",
                "field": "dc:identifier,preferred-name,affiliation-current,subject-area",
                "count": 10,
                "apiKey": self._api_key,
            },
            headers={"Accept": "application/json"},
            timeout=15,
        )
        search_resp.raise_for_status()

        entries = search_resp.json().get("search-results", {}).get("entry", [])
        if not entries or "error" in entries[0]:
            return {
                "h_index": None, "scopus_author_id": None,
                "source_url": "", "evidence_text": f"No Scopus results for {name}",
                "retrieved_at": now,
            }

        # Build alias list for this school from _INSTITUTION_KEYWORDS
        # Falls back to just the school name if not in map
        school_keyword = _INSTITUTION_KEYWORDS.get(school, school.lower())
        aliases = [school_keyword]

        # Step 2: Score every candidate
        scored = []
        for entry in entries:
            affil = entry.get("affiliation-current", {})
            if isinstance(affil, list):
                affil_str = " ".join(a.get("affiliation-name", "") for a in affil)
            elif isinstance(affil, dict):
                affil_str = affil.get("affiliation-name", "")
            else:
                affil_str = ""

            subject_data = entry.get("subject-area", [])
            if isinstance(subject_data, list):
                subjects = [s.get("$", "") for s in subject_data]
            elif isinstance(subject_data, dict):
                subjects = [subject_data.get("$", "")]
            else:
                subjects = []

            affil_s = _affiliation_score(affil_str, aliases)
            subj_s  = _subject_area_score(subjects, self._discipline)
            score   = min(affil_s + subj_s, 1.0)
            scored.append((score, affil_str, entry))

        scored.sort(key=lambda x: x[0], reverse=True)

        # Log top candidates
        print(f"      [Scopus] Top candidates for {name}:")
        for s, affil_str, entry in scored[:4]:
            n = entry.get("preferred-name", {})
            display = f"{n.get('given-name', '')} {n.get('surname', '')}".strip()
            label = "accepted" if s >= ACCEPT_THRESHOLD else ("needs_review" if s >= REVIEW_THRESHOLD else "discarded")
            print(f"             score={s:.2f} ({label})  affil='{affil_str}'  name='{display}'")

        best_score, best_affil, best_entry = scored[0]

        if best_score < REVIEW_THRESHOLD:
            return {
                "h_index": None, "scopus_author_id": None,
                "source_url": "", "evidence_text": f"No confident match found (best score={best_score:.2f})",
                "retrieved_at": now,
            }

        # Step 3: Fetch H-index for best match
        author_id = best_entry.get("dc:identifier", "").replace("AUTHOR_ID:", "")
        if not author_id:
            return {
                "h_index": None, "scopus_author_id": None,
                "source_url": "", "evidence_text": "Could not extract Scopus author ID",
                "retrieved_at": now,
            }

        print(f"      [Scopus] Fetching profile for author_id={author_id} (score={best_score:.2f})")
        profile_resp = requests.get(
            f"https://api.elsevier.com/content/author/author_id/{author_id}",
            params={"field": "h-index,document-count,citation-count", "apiKey": self._api_key},
            headers={"Accept": "application/json"},
            timeout=15,
        )
        profile_resp.raise_for_status()

        response_data = profile_resp.json().get("author-retrieval-response", [{}])[0]
        h_index_val = response_data.get("h-index")
        doc_count = response_data.get("coredata", {}).get("document-count", "?")
        h_index = int(h_index_val) if h_index_val is not None else None

        resolution = "accepted" if best_score >= ACCEPT_THRESHOLD else "needs_review"

        return {
            "h_index": h_index,
            "scopus_author_id": author_id,
            "source_url": f"https://www.scopus.com/authid/detail.uri?authorId={author_id}",
            "evidence_text": f"Scopus: h-index={h_index}, documents={doc_count}, matched_affil='{best_affil}', confidence={best_score:.2f}, resolution={resolution}",
            "retrieved_at": now,
        }
