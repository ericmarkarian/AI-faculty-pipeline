"""
Faculty Search Prototype
Targets: Pediatric Endocrinology faculty at Stanford
Agents: 1) Scraper (Responses API with live web search)
        2) Enricher (NIH Reporter API + Scopus API + Responses API web search)
        3) QC (Chat API)

Usage:
    export OPENAI_API_KEY="your-openai-key"
    export SCOPUS_API_KEY="your-scopus-key"
    pip install openai pandas requests
    python3 faculty_search.py
"""

import os
import json
import time
import asyncio
import sqlite3
import threading
import requests
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv
from openai import OpenAI
from concurrent.futures import ThreadPoolExecutor, as_completed

load_dotenv()

import observe
from providers.live import LiveNihProvider, LiveRolesProvider, LiveHIndexProvider, merge_specialty_considerations
from providers.generic_faculty_scraper import GenericFacultyScraper
from providers.profile_scrapers import StanfordProfileScraper, TavilyProfileScraper, PlaywrightProfileScraper
from providers.profile_scrapers.base import ProfileScraper
from providers.qc import verify_faculty_roles

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
SCOPUS_API_KEY = os.environ.get("SCOPUS_API_KEY")

SCHOOLS = ["Stanford University School of Medicine"]
DISCIPLINE = "Pediatric Endocrinology"
MAX_WORKERS = 8  # Max concurrent faculty enrichments

# CACHE_DB  = "faculty_cache.db"
# TTL_DAYS  = {"nih": 30, "h_index": 90, "roles": 7}


# Scopus affiliation keyword (lowercase, matched against affiliation string).
INSTITUTION_KEYWORDS: dict[str, str] = {
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
    "UC San Diego School of Medicine / Rady Children's Hospital":                      "san diego",
    "University of Pittsburgh School of Medicine / UPMC Children's Hospital":          "pittsburgh",
    "David Geffen School of Medicine at UCLA / Mattel Children's Hospital":            "ucla",
    "Vanderbilt University School of Medicine / Monroe Carell Jr. Children's":         "vanderbilt",
    "Medical University of South Carolina":                                            "south carolina",
    "UT Southwestern Medical Center / Children's Medical Center Dallas":               "southwestern",
    "Dell Medical School, University of Texas at Austin / Dell Children's":            "texas austin",
    "University of Utah School of Medicine / Intermountain Primary Children's":        "utah",
}


# ─────────────────────────────────────────────
# HELPER: Responses API web search
# ─────────────────────────────────────────────

def responses_web_search(prompt: str) -> str:
    t0 = time.time()
    observe.metric("api_calls", sub="openai_responses")
    try:
        response = client.responses.create(
            model="gpt-4o",
            tools=[{"type": "web_search_preview"}],
            input=[{"role": "user", "content": prompt}],
        )
        raw = ""
        for block in response.output:
            if hasattr(block, "content"):
                for item in block.content:
                    if hasattr(item, "text"):
                        raw += item.text
        cost = observe.add_cost("openai_responses_web_search")
        observe.log("api_call", provider="openai_responses",
                    latency_ms=int((time.time() - t0) * 1000),
                    cost_usd=round(cost, 4), status="ok")
        return raw.strip()
    except Exception as e:
        observe.metric("api_errors", sub="openai")
        observe.log("api_error", level="error", provider="openai_responses",
                    error=str(e), latency_ms=int((time.time() - t0) * 1000))
        raise


def parse_json(raw: str) -> any:
    clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        # Response may be truncated — extract the longest valid JSON array prefix
        start = clean.find("[")
        if start == -1:
            return []
        for end in range(len(clean), start, -1):
            try:
                return json.loads(clean[start:end])
            except json.JSONDecodeError:
                continue
        return []


# ─────────────────────────────────────────────
# CACHE — disabled for no-cache live run
# ─────────────────────────────────────────────
# _CACHE_WRITE_LOCK = threading.Lock()
# def init_cache(): ...
# def _is_fresh(ts, ttl_days): ...
# def cache_get(name, institution): ...
# def cache_update(name, institution, fields): ...



# ─────────────────────────────────────────────
# HELPER: Scopus API
# ─────────────────────────────────────────────

def get_scopus_hindex(full_name: str, school: str) -> int | None:
    """
    Queries Scopus Author Search API to find H-index.
    Steps:
      1. Search for author by name + affiliation
      2. Get the Scopus Author ID from top result
      3. Fetch full author profile to get H-index
    """
    if not SCOPUS_API_KEY:
        print("      [Scopus] No API key set — skipping")
        return None

    try:
        # Clean name — remove credentials
        parts = full_name.split()
        parts = [p for p in parts if not any(c in p for c in ["MD", "PhD", "DO", "MPH", "MBA", "FAAP", "MS", "DFAACAP"])]
        parts = [p.strip(".,") for p in parts if p.strip(".,")]
        if len(parts) < 2:
            return None

        first_name = parts[0]
        last_name = parts[-1]
        # Step 1: Search for author
        search_resp = requests.get(
            "https://api.elsevier.com/content/search/author",
            params={
                "query": f"AUTHLASTNAME({last_name}) AND AUTHFIRST({first_name})",
                "field": "dc:identifier,preferred-name,affiliation-current",
                "count": 10,
                "apiKey": SCOPUS_API_KEY
            },
            headers={"Accept": "application/json"},
            timeout=15
        )

        search_data = search_resp.json()
        entries = search_data.get("search-results", {}).get("entry", [])

        if not entries or "error" in entries[0]:
            print(f"      [Scopus] No author found for {full_name}")
            return None

        # Filter entries by institution keyword in affiliation name
        kw = INSTITUTION_KEYWORDS.get(school, school.split("/")[0].strip().lower())
        matched_entry = None
        for entry in entries:
            affil = entry.get("affiliation-current", {})
            if isinstance(affil, list):
                affil_name = " ".join(a.get("affiliation-name", "") for a in affil).lower()
            elif isinstance(affil, dict):
                affil_name = affil.get("affiliation-name", "").lower()
            else:
                affil_name = ""
            if kw in affil_name:
                matched_entry = entry
                break

        if not matched_entry:
            print(f"      [Scopus] No {school}-affiliated result found for {full_name} — returning None")
            return None

        author_id = matched_entry.get("dc:identifier", "").replace("AUTHOR_ID:", "")
        if not author_id:
            return None

        print(f"      [Scopus] Found author ID {author_id} for {full_name}")

        # Step 2: Fetch full author profile for H-index
        profile_resp = requests.get(
            f"https://api.elsevier.com/content/author/author_id/{author_id}",
            params={
                "field": "h-index,document-count,citation-count,coauthor-count",
                "apiKey": SCOPUS_API_KEY
            },
            headers={"Accept": "application/json"},
            timeout=15
        )

        profile_data = profile_resp.json()
        h_index = profile_data.get("author-retrieval-response", [{}])[0].get("h-index")

        if h_index is not None:
            return int(h_index)

    except Exception as e:
        print(f"      [Scopus] Error for {full_name}: {e}")

    return None


# ─────────────────────────────────────────────
# AGENT 1: SCRAPER
# ─────────────────────────────────────────────

_scraper_provider = GenericFacultyScraper()


def agent1_scrape(school_name: str, scraper=None, discipline: str = DISCIPLINE) -> list[dict]:
    t0 = time.time()
    observe.log("agent1_start", school=school_name, discipline=discipline)
    print(f"\n[Agent 1] Finding {school_name} {discipline} faculty...")

    provider = scraper or _scraper_provider
    all_faculty: list[dict] = []

    try:
        records = provider.get_faculty(school_name, discipline)
        all_faculty.extend(records)
        print(f"  -> Found {len(records)} faculty (Playwright + GPT-4o HTML extraction)")
    except ValueError:
        # No browse URL configured for this school — fall back to LLM web search
        print(f"  [Agent 1] No browse URL for {school_name}, falling back to web search...")
        prompt = f"""
You are a research assistant building a faculty recruitment database.

Search the web and find ALL {discipline} faculty at {school_name} School of Medicine.
Look at their official faculty directory page.

For each faculty member, extract:
- full_name
- title (e.g. Assistant Professor, Associate Professor, Professor, Instructor, Lecturer, Chair)
- subspecialty (if listed, else null)
- email (if listed, else null)
- profile_url (link to their individual faculty page, if available)

Return ONLY a JSON array of objects with those exact keys. No explanation, no markdown fences.
If a field is not available, use null.
"""
        try:
            raw = responses_web_search(prompt)
            faculty_list = parse_json(raw)
            for f in faculty_list:
                f["school"] = school_name
            all_faculty.extend(faculty_list)
            print(f"  -> Found {len(faculty_list)} faculty (web search fallback)")
            time.sleep(1)
        except Exception as e:
            observe.log("agent1_error", level="error", school=school_name, error=str(e))
            print(f"  [!] Failed to scrape {school_name}: {e}")
    except Exception as e:
        observe.log("agent1_error", level="error", school=school_name, error=str(e))
        print(f"  [!] Scraper failed for {school_name}: {e}")

    # Deduplicate
    _EXCLUDE_TITLE_KEYWORDS = ("emeritus", "emerita", "by courtesy", "courtesy")

    def _is_excluded(f: dict) -> bool:
        title = (f.get("title") or "").lower()
        return any(kw in title for kw in _EXCLUDE_TITLE_KEYWORDS)

    seen: set[str] = set()
    deduped = []
    for f in all_faculty:
        if _is_excluded(f):
            print(f"  [Agent 1] Skipping '{f.get('full_name')}' (title: {f.get('title')})")
            continue
        key = f.get("full_name", "").lower().strip()
        if key not in seen:
            seen.add(key)
            deduped.append(f)

    observe.log("agent1_complete", school=school_name, faculty_found=len(deduped),
                latency_ms=int((time.time() - t0) * 1000))
    print(f"  -> Total at {school_name}: {len(deduped)}")
    return deduped


# ─────────────────────────────────────────────
# AGENT 2: ENRICHER
# NIH Reporter + Scopus + web search
# ─────────────────────────────────────────────

_PROFILE_SCRAPERS: list[ProfileScraper] = [
    StanfordProfileScraper(),
    # TavilyProfileScraper(),  # original catch-all stub (no-op, fell through to LLM)
    PlaywrightProfileScraper(),  # fetches profile page + GPT-4o extraction
]


def _get_profile_scraper(profile_url: str) -> ProfileScraper | None:
    for scraper in _PROFILE_SCRAPERS:
        if scraper.handles(profile_url):
            return scraper
    return None


def agent2_enrich(
    faculty: dict,
    nih_provider=None,
    roles_provider=None,
    hindex_provider=None,
    on_enriched=None,
    discipline: str = DISCIPLINE,
) -> dict:
    if nih_provider is None:
        nih_provider = LiveNihProvider(discipline=discipline)
    if roles_provider is None:
        roles_provider = LiveRolesProvider(client)
    if hindex_provider is None:
        hindex_provider = LiveHIndexProvider(discipline=discipline)

    t0 = time.time()
    name = faculty.get("full_name", "Unknown")
    school = faculty.get("school", "")
    profile_url = faculty.get("profile_url") or ""

    print(f"  [Agent 2] Enriching: {name} ({school})")

    observe.metric("cache_misses_nih")
    observe.metric("cache_misses_roles")

    # Defaults
    nih_grants       = []
    editorial_roles  = []
    society_roles    = []
    training_roles   = []
    leadership_roles = []
    h_index          = None
    h_index_source   = None

    # H-index — Scopus first, OpenAlex fallback
    print(f"    -> Fetching H-index (Scopus → OpenAlex)...")
    try:
        hindex_data = hindex_provider.get_hindex(name, school)
        h_index = hindex_data.get("h_index")
        _ev = hindex_data.get("evidence_text") or ""
        if _ev.startswith("Scopus"):
            h_index_source = "Scopus"
        elif "OpenAlex" in _ev or "confidence=" in _ev:
            h_index_source = "OpenAlex"
        print(f"    -> H-index: {h_index} ({h_index_source or 'n/a'})")
    except Exception as e:
        print(f"    [!] H-index lookup failed: {e}")

    # Try profile scraper first for roles — free, deterministic, no LLM
    profile_scraper_roles: list | None = None
    profile_email: str | None = None
    profile_education: list = []
    if profile_url:
        scraper = _get_profile_scraper(profile_url)
        if scraper:
            try:
                profile_data = scraper.get_profile_data(profile_url)
                scraped = profile_data.get("roles") or []
                profile_email = profile_data.get("email") or None
                profile_education = profile_data.get("education") or []
                if scraped:
                    profile_scraper_roles = scraped
                    print(f"    -> [ProfileScraper] {len(scraped)} roles from profile page (no LLM)")
                if profile_email:
                    print(f"    -> [ProfileScraper] Email: {profile_email}")
                if profile_education:
                    print(f"    -> [ProfileScraper] {len(profile_education)} education entries")
            except Exception as e:
                print(f"    [!] ProfileScraper error, falling back to LLM: {e}")

    need_title = not faculty.get("title")  # True for manual lookup records
    fetch_keys = ["nih"] + ([] if profile_scraper_roles is not None else ["roles"])
    with ThreadPoolExecutor(max_workers=2) as _pool:
        futures_map = {"nih": _pool.submit(nih_provider.get_grants, name, school)}
        if profile_scraper_roles is None:
            # Use get_roles_and_title when we need the rank (manual lookup), get_roles otherwise
            if need_title:
                futures_map["roles"] = _pool.submit(roles_provider.get_roles_and_title, name, school, profile_url)
            else:
                futures_map["roles"] = _pool.submit(roles_provider.get_roles, name, school, profile_url)
        fetch_results = []
        for key in fetch_keys:
            try:
                fetch_results.append(futures_map[key].result())
            except Exception as exc:
                fetch_results.append(exc)

    # Process profile scraper roles if available
    if profile_scraper_roles is not None:
        all_roles        = profile_scraper_roles
        editorial_roles  = [r for r in all_roles if r.get("category") == "editorial"]
        society_roles    = [r for r in all_roles if r.get("category") == "society"]
        training_roles   = [r for r in all_roles if r.get("category") == "training"]
        leadership_roles = [r for r in all_roles if r.get("category") == "leadership"]
        print(f"    -> [ProfileScraper] Editorial: {len(editorial_roles)}, Society: {len(society_roles)}, Training: {len(training_roles)}, Leadership: {len(leadership_roles)}")

    extracted_title: str | None = None
    for key, result in zip(fetch_keys, fetch_results):
        if key == "nih":
            if isinstance(result, Exception):
                print(f"    [!] NIH lookup failed for {name}: {result}")
            else:
                nih_grants = result
                print(f"    -> NIH grants found: {len(nih_grants)}")
                for g in nih_grants:
                    print(f"       {g['project_num']} — {g['title']}")
        elif key == "roles":
            if isinstance(result, Exception):
                print(f"    [!] Roles search failed for {name}: {result}")
            else:
                # get_roles_and_title returns (title, roles, discovered_urls); get_roles returns just roles
                discovered_profile_urls: list[str] = []
                if need_title and isinstance(result, tuple):
                    extracted_title, all_roles, discovered_profile_urls = result
                    if extracted_title:
                        print(f"    -> [LLM] Title found: {extracted_title}")
                    for u in discovered_profile_urls:
                        print(f"    -> [LLM] Profile URL discovered: {u}")
                else:
                    all_roles = result
                editorial_roles  = [r for r in all_roles if r.get("category") == "editorial"]
                society_roles    = [r for r in all_roles if r.get("category") == "society"]
                training_roles   = [r for r in all_roles if r.get("category") == "training"]
                leadership_roles = [r for r in all_roles if r.get("category") == "leadership"]
                print(f"    -> [LLM] Editorial: {len(editorial_roles)}, Society: {len(society_roles)}, Training: {len(training_roles)}, Leadership: {len(leadership_roles)}")

                # Collect all candidate profile URLs to scrape for email/education
                if not profile_email and not profile_education:
                    _PROFILE_URL_PATTERNS = (
                        "/people/", "/faculty/", "/profile", "/bio/", "/staff/",
                        "profiles.", "faculty.", "directory.",
                    )
                    last_lower = name.split()[-1].lower() if name.split() else ""

                    # Build ordered list: LLM-discovered URLs first, then role source_urls
                    candidate_urls: list[str] = list(discovered_profile_urls)
                    seen = {u.lower() for u in candidate_urls}
                    for r in all_roles:
                        su = r.get("source_url") or ""
                        if su.lower() not in seen and any(p in su.lower() for p in _PROFILE_URL_PATTERNS):
                            candidate_urls.append(su)
                            seen.add(su.lower())

                    for candidate_url in candidate_urls:
                        scraper = _get_profile_scraper(candidate_url)
                        if not scraper:
                            continue
                        try:
                            pd = scraper.get_profile_data(candidate_url)
                            if not profile_email:
                                profile_email = pd.get("email") or None
                            if not profile_education:
                                profile_education = pd.get("education") or []
                            if profile_email:
                                print(f"    -> [ProfileScraper/LLM-URL] Email: {profile_email} (from {candidate_url})")
                            if profile_education:
                                print(f"    -> [ProfileScraper/LLM-URL] {len(profile_education)} education entries (from {candidate_url})")
                        except Exception as e:
                            print(f"    [!] ProfileScraper on {candidate_url} failed: {e}")
                        if profile_email and profile_education:
                            break  # got everything we need

    nih_project_nums = ", ".join(g["project_num"] for g in nih_grants)
    nih_grant_titles = " | ".join(g["title"] for g in nih_grants)

    nih_grant_count = len(nih_grants)
    nih_institution_unconfirmed = any(g.get("institution_unconfirmed") for g in nih_grants)

    # --- Observability ---
    latency_ms = int((time.time() - t0) * 1000)
    observe.metric("faculty_enriched")
    observe.metric("latencies_ms", value=latency_ms)
    if nih_grants:       observe.metric("field_coverage", sub="nih_grants")
    if editorial_roles:  observe.metric("field_coverage", sub="editorial")
    if society_roles:    observe.metric("field_coverage", sub="society")
    if training_roles:   observe.metric("field_coverage", sub="training")
    if leadership_roles: observe.metric("field_coverage", sub="leadership")
    observe.log("faculty_enriched", faculty=name, school=school,
                nih_grants=nih_grant_count,
                editorial=len(editorial_roles), society=len(society_roles),
                training=len(training_roles), leadership=len(leadership_roles),
                cache_nih=False, cache_roles=False,
                latency_ms=latency_ms)

    return {
        **faculty,
        "title": faculty.get("title") or extracted_title,
        "email": faculty.get("email") or profile_email,
        "education": profile_education or [],
        "h_index": h_index,
        "h_index_source": h_index_source,
        "nih_project_nums":  nih_project_nums,
        "nih_grant_titles":  nih_grant_titles,
        "nih_grant_count":   nih_grant_count,
        "nih_institution_unconfirmed": nih_institution_unconfirmed,
        "editorial_roles":   editorial_roles,
        "society_roles":     society_roles,
        "training_roles":    training_roles,
        "leadership_roles":  leadership_roles,
    }


# ─────────────────────────────────────────────
# AGENT 3: QC
# RAGAS-style faithfulness verification of role claims.
# For each faculty member:
#   1. URL liveness  — does source_url actually resolve?
#   2. Faithfulness  — does evidence_text actually confirm the role?
# Produces green / yellow / red per category and an overall signal.
# ─────────────────────────────────────────────

def _qc_cross_checks(results: list[dict]) -> None:
    """
    Run cohort-level QC checks after per-person verification:
      1. Duplicate unique roles — flag if two people claim the same
         singleton role (e.g. Division Chief) at the same org.
      2. Completeness — flag high-profile faculty (h_index ≥ 30 or
         nih_grant_count ≥ 5) who have zero roles found; likely a
         scraping failure rather than genuinely no service activity.
    Findings are printed and written into each faculty's qc dict
    under "warnings".
    """
    # ── 1. Duplicate unique-role detection ───────────────────────────
    SINGLETON_KEYWORDS = {
        "chief", "chair", "president", "director", "dean",
        "program director", "fellowship director",
    }

    # role_key → list of (name, category)
    from collections import defaultdict
    role_holders: dict[str, list[str]] = defaultdict(list)

    for f in results:
        name = f.get("full_name", "?")
        for detail in (f.get("qc") or {}).get("role_details", []):
            role_lower = (detail.get("role") or "").lower()
            org_lower  = (detail.get("org") or "").lower()
            if any(kw in role_lower for kw in SINGLETON_KEYWORDS):
                key = f"{role_lower} @ {org_lower}"
                role_holders[key].append(name)

    duplicate_warnings: dict[str, list[str]] = {
        k: v for k, v in role_holders.items() if len(v) > 1
    }

    if duplicate_warnings:
        print("\n  [QC] ⚠ DUPLICATE SINGLETON ROLES DETECTED:")
        for role_key, holders in duplicate_warnings.items():
            print(f"       '{role_key}' claimed by: {', '.join(holders)}")

    # Write duplicate warnings back into each affected faculty's qc dict
    for f in results:
        name = f.get("full_name", "?")
        warnings = []
        for role_key, holders in duplicate_warnings.items():
            if name in holders:
                others = [h for h in holders if h != name]
                warnings.append(
                    f"Role '{role_key}' also claimed by: {', '.join(others)}"
                )
        if warnings and f.get("qc"):
            f["qc"]["warnings"] = f["qc"].get("warnings", []) + warnings

    # ── 2. Completeness check ─────────────────────────────────────────
    print("\n  [QC] Completeness check:")
    for f in results:
        name        = f.get("full_name", "?")
        h_index     = f.get("h_index") or 0
        grant_count = f.get("nih_grant_count") or 0
        role_count  = sum(
            len(f.get(f"{cat}_roles") or [])
            for cat in ("editorial", "society", "leadership", "training")
        )

        is_senior = h_index >= 30 or grant_count >= 5
        if is_senior and role_count == 0:
            msg = (
                f"SPARSE PROFILE — h_index={h_index}, grants={grant_count} "
                f"but 0 roles found. Likely scraping failure."
            )
            print(f"       ⚠ {name}: {msg}")
            if f.get("qc"):
                f["qc"]["warnings"] = f["qc"].get("warnings", []) + [msg]
                # Downgrade overall from green to yellow if vacuously green
                if f["qc"].get("overall") == "green":
                    f["qc"]["overall"] = "yellow"
        else:
            print(f"       ✓ {name}: {role_count} role(s), h={h_index}, grants={grant_count}")


def agent3_qc(faculty_list: list[dict]) -> list[dict]:
    print(f"\n[Agent 3] Running QC on {len(faculty_list)} faculty members...")

    results = []
    green = yellow = red = 0

    for faculty in faculty_list:
        name = faculty.get("full_name", "Unknown")
        try:
            qc = verify_faculty_roles(faculty, client)
            faculty = {**faculty, "qc": qc}

            overall = qc["overall"]
            if overall == "green":   green  += 1
            elif overall == "yellow": yellow += 1
            else:                     red    += 1

            role_count = len(qc.get("role_details", []))
            print(f"  [QC] {name}: {overall.upper()} ({role_count} roles checked)")
        except Exception as e:
            print(f"  [QC] Failed for {name}: {e}")
            faculty = {**faculty, "qc": {"overall": "yellow", "error": str(e)}}

        results.append(faculty)

    # Cohort-level checks
    _qc_cross_checks(results)

    # Recount after cross-checks may have downgraded some
    green = yellow = red = 0
    for f in results:
        o = (f.get("qc") or {}).get("overall", "yellow")
        if o == "green":    green  += 1
        elif o == "yellow": yellow += 1
        else:               red    += 1

    print(f"\n  -> QC complete. Green: {green}  Yellow: {yellow}  Red: {red}")
    return results


# ─────────────────────────────────────────────
# SUMMARY GENERATION
# ─────────────────────────────────────────────

def _parse_roles(value) -> list:
    """Return a list of role dicts from either a Python list or a JSON string."""
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def generate_faculty_summary(record: dict) -> str:
    """
    Build a short prose paragraph for one faculty member using only the
    data already collected by the pipeline — no new searches.
    """
    name  = record.get("full_name") or "This faculty member"
    title = record.get("title") or ""
    school = record.get("school") or ""

    sentences = []

    # Sentence 1 — role & institution
    article = "an" if title and title[0].lower() in "aeiou" else "a"
    if title and school:
        sentences.append(f"{name} is {article} {title} at {school}.")
    elif school:
        sentences.append(f"{name} is at {school}.")
    else:
        sentences.append(f"{name} is a faculty member.")

    # Sentence 2 — H-index
    h = record.get("h_index")
    if h:
        sentences.append(f"Their H-index is {h}.")

    # Sentence 3 — NIH grants
    grant_count = int(record.get("nih_grant_count") or 0)
    if grant_count > 0:
        raw_titles = record.get("nih_grant_titles") or ""
        titles = [t.strip() for t in raw_titles.split("|") if t.strip()]
        if titles:
            shown = titles[:2]
            quoted = ", ".join(f'"{t}"' for t in shown)
            extra = grant_count - len(shown)
            more = f", and {extra} more" if extra > 0 else ""
            sentences.append(f"They hold {grant_count} active NIH grant{'s' if grant_count != 1 else ''}, including {quoted}{more}.")
        else:
            sentences.append(f"They hold {grant_count} active NIH grant{'s' if grant_count != 1 else ''}.")

    # Sentence 4 — professional societies
    society_roles = _parse_roles(record.get("society_roles"))
    if society_roles:
        orgs = list(dict.fromkeys(r.get("org", "") for r in society_roles if r.get("org")))
        if orgs:
            if len(orgs) == 1:
                sentences.append(f"They are a member of the {orgs[0]}.")
            else:
                joined = ", ".join(orgs[:-1]) + f", and {orgs[-1]}"
                sentences.append(f"They are a member of the {joined}.")

    # Sentence 5 — editorial boards (optional)
    editorial_roles = _parse_roles(record.get("editorial_roles"))
    if editorial_roles:
        orgs = list(dict.fromkeys(r.get("org", "") for r in editorial_roles if r.get("org")))
        if orgs:
            joined = ", ".join(orgs[:2])
            sentences.append(f"They serve on the editorial board of {joined}.")

    # Sentence 6 — leadership roles (optional)
    leadership_roles = _parse_roles(record.get("leadership_roles"))
    if leadership_roles:
        first = leadership_roles[0]
        role_str = first.get("role", "")
        org_str  = first.get("org", "")
        if role_str and org_str:
            sentences.append(f"They hold the role of {role_str} at {org_str}.")

    return " ".join(sentences)


# ─────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────

def run_pipeline_from_records(records, on_enriched=None, on_qc_complete=None, roles_provider=None, discipline: str = DISCIPLINE, considerations: str = ""):
    """Run Agent 2 (enrichment) + Agent 3 (QC) on a pre-built list of faculty records.

    Records must have at minimum: full_name, school.
    Returns the final QC'd list.
    """
    observe.start()
    if not records:
        print("\n[!] No records provided.")
        return []

    if considerations and roles_provider is None:
        if discipline:
            # Mass search: merge into the specialty .md file permanently
            merge_specialty_considerations(discipline, considerations, client)
            roles_provider = LiveRolesProvider(client, discipline=discipline)
        else:
            # Manual lookup: no specialty — inject considerations as one-time prompt guidance
            roles_provider = LiveRolesProvider(client, discipline="", considerations=considerations)

    _enrich_start = time.time()
    n = len(records)
    print(f"\n[Agent 2] Enriching {n} faculty profiles ({MAX_WORKERS} workers)...")
    enriched = [None] * n
    _print_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(agent2_enrich, f, roles_provider=roles_provider, discipline=discipline): i for i, f in enumerate(records)}
        for completed, future in enumerate(as_completed(futures), 1):
            idx = futures[future]
            result = future.result()
            result["summary"] = generate_faculty_summary(result)
            enriched[idx] = result
            with _print_lock:
                print(f"  [Progress] {completed}/{n} — {result.get('full_name', 'Unknown')}")
            if on_enriched:
                on_enriched(result, completed, n)

    _enrich_elapsed = time.time() - _enrich_start
    print(f"\n[Timing] Enrichment: {_enrich_elapsed:.1f}s for {n} faculty ({_enrich_elapsed/n:.1f}s avg)")

    final = agent3_qc(enriched)

    if on_qc_complete:
        for record in final:
            on_qc_complete(record)

    return final


def run_pipeline(schools=None, discipline=None, on_enriched=None, on_qc_complete=None, considerations: str = ""):
    t_start = time.time()
    observe.start()
    _schools = schools or SCHOOLS
    _discipline = discipline or DISCIPLINE
    observe.log("pipeline_start", discipline=_discipline, schools=_schools)

    # Check required env vars
    if not SCOPUS_API_KEY:
        print("Warning: SCOPUS_API_KEY not set — H-index will be skipped.")
        print("Run: export SCOPUS_API_KEY='your-scopus-key'")

    if considerations:
        merge_specialty_considerations(_discipline, considerations, client)
    _roles_provider = LiveRolesProvider(client, discipline=_discipline)

    all_faculty = []

    for school in _schools:
        faculty = agent1_scrape(school, discipline=_discipline)
        all_faculty.extend(faculty)
        time.sleep(1)

    # Global deduplicate
    seen = set()
    deduped = []
    for f in all_faculty:
        key = (f.get("full_name", "").lower().strip(), f.get("school", "").lower())
        if key not in seen:
            seen.add(key)
            deduped.append(f)

    print(f"\n[Pipeline] Total faculty to enrich: {len(deduped)}")

    if not deduped:
        print("\n[!] No faculty found. Check your API key and try again.")
        return

    final = run_pipeline_from_records(deduped, on_enriched=on_enriched, on_qc_complete=on_qc_complete, roles_provider=_roles_provider, discipline=_discipline, considerations="")

    # Flatten nested qc dict into 3 consolidated CSV columns: each is
    # "<confidence> — <why>", with the roles column listing every role + link + why.
    def _compose(conf: str, note: str) -> str:
        return f"{conf} — {note}" if note else (conf or "")

    _ROLE_CATS = ("editorial", "society", "leadership", "training")
    for record in final:
        qc = record.get("qc") or {}
        record["qc_nih"]    = _compose(qc.get("nih_confidence", ""), qc.get("nih_note", ""))
        record["qc_hindex"] = _compose(qc.get("hindex_confidence", ""), qc.get("hindex_note", ""))
        roles_conf = qc.get("overall", "")  # worst across role categories already folded into overall
        # roles-only worst (exclude nih/hindex) for an accurate roles header
        roles_only = [qc.get(f"{c}_confidence", "green") for c in _ROLE_CATS]
        roles_conf = "red" if "red" in roles_only else ("yellow" if "yellow" in roles_only else "green")
        record["qc_roles"] = _compose(roles_conf, qc.get("roles_summary", ""))

    df = pd.DataFrame(final)
    df["nih_project_nums"] = df["nih_project_nums"].fillna("")
    df["nih_grant_titles"] = df["nih_grant_titles"].fillna("")
    df["nih_grant_count"] = df["nih_grant_count"].fillna(0).astype(int)
    df.to_csv("faculty_output.csv", index=False)
    with open("faculty_output.json", "w") as f:
        json.dump(final, f, indent=2)
    with open("faculty_summaries.txt", "w") as f:
        for record in final:
            f.write(f"{record.get('full_name', 'Unknown')} ({record.get('school', '')})\n")
            f.write(record["summary"] + "\n\n")

    runtime = time.time() - t_start

    # Alert checks
    alerts = observe.run_alert_checks(len(final), _discipline)
    if alerts:
        print("\n[!] ALERTS:")
        for a in alerts:
            print(f"    {a}")

    # Final metrics snapshot
    m = observe.get_metrics()
    observe.log("pipeline_complete", discipline=_discipline, schools=_schools,
                faculty_count=len(final), runtime_seconds=round(runtime, 2),
                llm_cost_usd=round(m["llm_cost_usd"], 4),
                cache_hit_rate_nih=m["cache_hit_rate_nih"],
                cache_hit_rate_roles=m["cache_hit_rate_roles"],
                api_errors=sum(m["api_errors"].values()),
                alerts=len(alerts))

    print("\n Done! Results saved to:")
    print(f"   faculty_output.csv       ({len(final)} faculty members)")
    print(f"   faculty_output.json")
    print(f"   faculty_summaries.txt    ({len(final)} paragraph summaries)")
    print(f"   logs/pipeline_{observe.RUN_ID}.jsonl")

    # Coverage stats
    h_found = sum(1 for f in final if f.get("h_index"))
    nih_found = sum(1 for f in final if f.get("nih_project_nums"))
    leadership_found = sum(1 for f in final if f.get("leadership_roles"))
    qc_green  = sum(1 for f in final if (f.get("qc") or {}).get("overall") == "green")
    qc_yellow = sum(1 for f in final if (f.get("qc") or {}).get("overall") == "yellow")
    qc_red    = sum(1 for f in final if (f.get("qc") or {}).get("overall") == "red")
    print(f"\n Coverage:")
    print(f"   H-index:        {h_found}/{len(final)}")
    print(f"   NIH grants:     {nih_found}/{len(final)}")
    print(f"   Leadership:     {leadership_found}/{len(final)}")
    print(f"\n QC Role Confidence:")
    print(f"   Green  (verified):      {qc_green}")
    print(f"   Yellow (needs review):  {qc_yellow}")
    print(f"   Red    (unverified):    {qc_red}")
    print(f"\n Observability:")
    print(f"   Run ID:         {observe.RUN_ID}")
    print(f"   Runtime:        {runtime:.1f}s")
    print(f"   LLM cost:       ${m['llm_cost_usd']:.4f}")
    print(f"   API errors:     {sum(m['api_errors'].values())}")
    print(f"   Cache hit (NIH):   {m['cache_hit_rate_nih']:.0%}")
    print(f"   Cache hit (roles): {m['cache_hit_rate_roles']:.0%}")

    summary_cols = ["full_name", "school", "title", "h_index", "nih_grant_count", "qc_nih", "qc_hindex", "qc_roles"]
    available = [c for c in summary_cols if c in df.columns]
    print("\n--- PREVIEW (first 10 rows) ---")
    print(df[available].head(10).to_string(index=False))


if __name__ == "__main__":
    if not os.environ.get("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY not set.")
        print("Run: export OPENAI_API_KEY='your-openai-key'")
    else:
        run_pipeline()
