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
import requests
import pandas as pd
from openai import OpenAI

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
SCOPUS_API_KEY = os.environ.get("SCOPUS_API_KEY")

SCHOOLS = ["Stanford"]
DISCIPLINE = "Pediatric Endocrinology"

INSTITUTION_MAP = {
    "Stanford": ["Stanford University", "Stanford"],
    "UCSF": ["University of California San Francisco", "UCSF"]
}

# Scopus parent AF-IDs (for reference — not used in author search API)
INSTITUTION_AFID = {
    "Stanford": "60012708",
    "UCSF": "60031970",
}

# Keywords to match against affiliation-current name returned by Scopus author search
INSTITUTION_KEYWORDS = {
    "Stanford": "stanford",
    "UCSF": "san francisco",
}


# ─────────────────────────────────────────────
# HELPER: Responses API web search
# ─────────────────────────────────────────────

def responses_web_search(prompt: str) -> str:
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
    return raw.strip()


def parse_json(raw: str) -> any:
    clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(clean)


# ─────────────────────────────────────────────
# HELPER: NIH Reporter API
# ─────────────────────────────────────────────

def get_nih_grants(full_name: str, school: str) -> list[str]:
    """Queries NIH Reporter API for active grants by PI name and institution."""
    try:
        parts = full_name.split()
        parts = [p for p in parts if not any(c in p for c in ["MD", "PhD", "DO", "MPH", "MBA", "FAAP", "MS"])]
        parts = [p.strip(".,") for p in parts if p.strip(".,")]

        if len(parts) < 2:
            return []

        first_name = parts[0]
        last_name = parts[-1]
        institutions = INSTITUTION_MAP.get(school, [school])

        payload = {
            "criteria": {
                "pi_names": [{"first_name": first_name, "last_name": last_name}],
                "org_names": institutions,
                "project_start_date": {"from_date": "2020-01-01"},
            },
            "fields": ["project_title", "fiscal_year", "award_amount", "project_start_date", "project_end_date"],
            "limit": 10,
            "offset": 0
        }

        resp = requests.post(
            "https://api.reporter.nih.gov/v2/projects/search",
            json=payload,
            timeout=15
        )
        data = resp.json()
        results = data.get("results", [])
        grants = [r.get("project_title", "Unknown grant") for r in results if r.get("project_title")]
        return grants

    except Exception as e:
        print(f"      [NIH] Error for {full_name}: {e}")
        return []


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
        affiliation = INSTITUTION_MAP.get(school, [school])[0]

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

        # Filter entries by "stanford" in affiliation name
        matched_entry = None
        for entry in entries:
            affil = entry.get("affiliation-current", {})
            if isinstance(affil, list):
                affil_name = " ".join(a.get("affiliation-name", "") for a in affil).lower()
            elif isinstance(affil, dict):
                affil_name = affil.get("affiliation-name", "").lower()
            else:
                affil_name = ""
            if school.lower() in affil_name:
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

def agent1_scrape(school_name: str) -> list[dict]:
    print(f"\n[Agent 1] Finding {school_name} {DISCIPLINE} faculty...")

    all_faculty = []

    prompt = f"""
You are a research assistant building a faculty recruitment database.

Search the web and find ALL {DISCIPLINE} faculty at {school_name} School of Medicine.
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
        print(f"  -> Found {len(faculty_list)} faculty")
        time.sleep(1)
    except Exception as e:
        print(f"  [!] Failed to scrape {school_name}: {e}")

    # Deduplicate
    seen = set()
    deduped = []
    for f in all_faculty:
        key = f.get("full_name", "").lower().strip()
        if key not in seen:
            seen.add(key)
            deduped.append(f)

    print(f"  -> Total at {school_name}: {len(deduped)}")
    return deduped


# ─────────────────────────────────────────────
# AGENT 2: ENRICHER
# NIH Reporter + Scopus + web search
# ─────────────────────────────────────────────

def agent2_enrich(faculty: dict) -> dict:
    name = faculty.get("full_name", "Unknown")
    school = faculty.get("school", "")
    profile_url = faculty.get("profile_url") or ""

    print(f"  [Agent 2] Enriching: {name} ({school})")

    # --- H-index via Scopus ---
    print(f"    -> Scopus API...")
    h_index = get_scopus_hindex(name, school)
    print(f"    -> H-index: {h_index}")
    time.sleep(0.3)

    # --- NIH Grants disabled for now ---
    # nih_grants = get_nih_grants(name, school)

    # --- Web search for roles disabled for now ---
    # prompt = f"..."
    # roles = responses_web_search(prompt)

    return {
        **faculty,
        "h_index": h_index,
    }


# ─────────────────────────────────────────────
# AGENT 3: QC
# ─────────────────────────────────────────────

def agent3_qc(faculty_list: list[dict]) -> list[dict]:
    print("\n[Agent 3] Running QC...")

    prompt = f"""
You are a data quality analyst reviewing a faculty recruitment database.

Here is a JSON array of faculty members:
{json.dumps(faculty_list, indent=2)}

Your tasks:
1. Flag likely duplicates (same person listed twice) — add "duplicate_flag": true and "duplicate_note": "reason"
2. Flag suspicious or conflicting data — add "conflict_flag": true and "conflict_note": "reason"
3. Add "confidence_score" (0-100) for how accurate and complete each entry seems
4. Add "qc_notes" summarizing any concerns

Return the FULL updated JSON array with these fields added to every entry.
No markdown, no explanation — just the JSON array.
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=8000
        )
        raw = response.choices[0].message.content
        clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        qc_list = json.loads(clean)
        flagged = sum(1 for f in qc_list if f.get("duplicate_flag") or f.get("conflict_flag"))
        print(f"  -> QC complete. {flagged} entries flagged.")
        return qc_list
    except Exception as e:
        print(f"  [!] QC failed: {e}")
        return faculty_list


# ─────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────

def run_pipeline():
    # Check required env vars
    if not SCOPUS_API_KEY:
        print("Warning: SCOPUS_API_KEY not set — H-index will be skipped.")
        print("Run: export SCOPUS_API_KEY='your-scopus-key'")

    all_faculty = []

    for school in SCHOOLS:
        faculty = agent1_scrape(school)
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

    print("\n[Agent 2] Enriching faculty profiles...")
    enriched = []
    for faculty in deduped:
        result = agent2_enrich(faculty)
        enriched.append(result)
        time.sleep(1)

    final = enriched  # Agent 3 QC disabled for now

    df = pd.DataFrame(final)
    df.to_csv("faculty_output.csv", index=False)
    with open("faculty_output.json", "w") as f:
        json.dump(final, f, indent=2)

    print(f"\n✅ Done! Results saved to:")
    print(f"   faculty_output.csv  ({len(final)} faculty members)")
    print(f"   faculty_output.json")

    # Coverage stats
    h_found = sum(1 for f in final if f.get("h_index"))
    nih_found = sum(1 for f in final if f.get("nih_grants"))
    leadership_found = sum(1 for f in final if f.get("leadership_roles"))
    print(f"\n📊 Coverage:")
    print(f"   H-index:        {h_found}/{len(final)}")
    print(f"   NIH grants:     {nih_found}/{len(final)}")
    print(f"   Leadership:     {leadership_found}/{len(final)}")

    summary_cols = ["full_name", "school", "title", "h_index", "confidence_score", "duplicate_flag", "conflict_flag"]
    available = [c for c in summary_cols if c in df.columns]
    print("\n--- PREVIEW (first 10 rows) ---")
    print(df[available].head(10).to_string(index=False))


if __name__ == "__main__":
    if not os.environ.get("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY not set.")
        print("Run: export OPENAI_API_KEY='your-openai-key'")
    else:
        run_pipeline()
