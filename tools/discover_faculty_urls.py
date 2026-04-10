"""
tools/discover_faculty_urls.py

One-time tool that discovers faculty directory URLs for a list of medical schools
and a given discipline. Results are written to data/faculty_urls.json for use
by the pipeline at runtime.

Usage:
    python3 tools/discover_faculty_urls.py \
        --discipline "Pediatric Endocrinology" \
        --output data/faculty_urls.json

How it works:
    1. For each school, asks GPT-4o (with web search) to find the faculty directory URL.
    2. Makes a quick HTTP HEAD/GET to verify the URL is reachable.
    3. Writes results to the output JSON, flagging unresolved schools for manual review.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# ---------------------------------------------------------------------------
# Top-10 US medical schools (US News 2024 research rankings)
# ---------------------------------------------------------------------------
TOP_10_MED_SCHOOLS: list[dict] = [
    # Original top-10 (already verified — will be skipped if present in output JSON)
    {"name": "Harvard Medical School",                                          "city": "Boston",         "state": "MA"},
    {"name": "Johns Hopkins School of Medicine",                                "city": "Baltimore",      "state": "MD"},
    {"name": "University of Pennsylvania Perelman School of Medicine",          "city": "Philadelphia",   "state": "PA"},
    {"name": "Columbia University Vagelos College of Physicians and Surgeons",  "city": "New York",       "state": "NY"},
    {"name": "Stanford University School of Medicine",                          "city": "Stanford",       "state": "CA"},
    {"name": "University of California San Francisco School of Medicine",       "city": "San Francisco",  "state": "CA"},
    {"name": "Duke University School of Medicine",                              "city": "Durham",         "state": "NC"},
    {"name": "Washington University in St. Louis School of Medicine",           "city": "St. Louis",      "state": "MO"},
    {"name": "University of Michigan Medical School",                           "city": "Ann Arbor",      "state": "MI"},
    {"name": "Yale School of Medicine",                                         "city": "New Haven",      "state": "CT"},
    # Expanded list — major pediatric academic medical centers
    {"name": "University of Colorado School of Medicine / Children's Hospital Colorado", "city": "Aurora",        "state": "CO"},
    {"name": "University of Cincinnati College of Medicine / Cincinnati Children's",     "city": "Cincinnati",    "state": "OH"},
    {"name": "Baylor College of Medicine / Texas Children's Hospital",                  "city": "Houston",       "state": "TX"},
    {"name": "University of Washington School of Medicine / Seattle Children's",        "city": "Seattle",       "state": "WA"},
    {"name": "Children's National Hospital / George Washington University",             "city": "Washington",    "state": "DC"},
    {"name": "Northwestern University Feinberg School of Medicine / Lurie Children's", "city": "Chicago",       "state": "IL"},
    {"name": "NIH National Institute of Child Health and Human Development (NICHD)",   "city": "Bethesda",      "state": "MD"},
    {"name": "Keck School of Medicine USC / Children's Hospital Los Angeles",          "city": "Los Angeles",   "state": "CA"},
    {"name": "Emory University School of Medicine / Children's Healthcare of Atlanta", "city": "Atlanta",       "state": "GA"},
    {"name": "University of Florida College of Medicine",                              "city": "Gainesville",   "state": "FL"},
    {"name": "Ohio State University College of Medicine / Nationwide Children's",      "city": "Columbus",      "state": "OH"},
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; FacultyPipelineBot/1.0; "
        "+https://github.com/your-org/ai-faculty-pipeline)"
    )
}

_SEARCH_PROMPT = """\
I need the exact URL of the official faculty directory page for the {discipline} \
division (or equivalent) at {school_name}.

The page should list individual faculty members with their names and titles — \
not a general department homepage, not a research page, not a news page.

Requirements:
- The URL must be from the official medical school or hospital domain
- The page should be publicly accessible (no login required)
- It should list multiple faculty members, not just one person

Return ONLY a JSON object with these fields and nothing else:
{{
  "url": "<the faculty directory URL or null if not found>",
  "notes": "<brief description of the page or reason it could not be found>"
}}
"""


def _search_for_url(client: OpenAI, school_name: str, discipline: str) -> dict:
    """Ask GPT-4o with web search to find the faculty directory URL."""
    prompt = _SEARCH_PROMPT.format(school_name=school_name, discipline=discipline)

    response = client.responses.create(
        model="gpt-4o",
        tools=[{"type": "web_search_preview"}],
        input=prompt,
    )

    # Extract text from response
    raw = ""
    for block in response.output:
        if hasattr(block, "content"):
            for part in block.content:
                if hasattr(part, "text"):
                    raw += part.text

    # Parse JSON from the response
    raw = raw.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract JSON from mixed text
        import re
        match = re.search(r'\{[^{}]+\}', raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {"url": None, "notes": f"Failed to parse response: {raw[:200]}"}


def _verify_url(url: str, timeout: int = 10) -> bool:
    """Return True if the URL responds with a 2xx or 3xx status."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
        return resp.status_code < 400
    except Exception:
        return False


def _load_existing(output_path: Path) -> dict:
    if output_path.exists():
        return json.loads(output_path.read_text())
    return {}


def _save(output_path: Path, data: dict) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover faculty directory URLs for medical schools")
    parser.add_argument("--discipline", default="Pediatric Endocrinology",
                        help="Clinical discipline to search for (default: Pediatric Endocrinology)")
    parser.add_argument("--output", default="data/faculty_urls.json",
                        help="Output JSON file path (default: data/faculty_urls.json)")
    parser.add_argument("--schools", nargs="*",
                        help="Subset of school names to process (default: all top 10)")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="Seconds to wait between API calls (default: 2.0)")
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    client = OpenAI(api_key=api_key)
    output_path = Path(args.output)
    discipline = args.discipline

    # Load existing results so re-runs are additive
    results: dict = _load_existing(output_path)
    results.setdefault(discipline, {})

    schools = TOP_10_MED_SCHOOLS
    if args.schools:
        schools = [s for s in schools if s["name"] in args.schools]

    print(f"\nDiscovering faculty URLs for: {discipline}")
    print(f"Schools: {len(schools)}")
    print(f"Output:  {output_path}\n")
    print("-" * 60)

    for school in schools:
        name = school["name"]

        # Skip if already resolved and verified
        existing = results[discipline].get(name, {})
        if existing.get("verified"):
            print(f"[SKIP]  {name} — already verified: {existing['url']}")
            continue

        print(f"[SEARCH] {name} ...", flush=True)

        result = _search_for_url(client, name, discipline)
        url = result.get("url")
        notes = result.get("notes", "")

        if url:
            print(f"  Found: {url}")
            print(f"  Verifying...", end=" ", flush=True)
            reachable = _verify_url(url)
            print("OK" if reachable else "UNREACHABLE")

            results[discipline][name] = {
                "url": url,
                "notes": notes,
                "verified": reachable,
                "needs_review": not reachable,
            }
        else:
            print(f"  Not found: {notes}")
            results[discipline][name] = {
                "url": None,
                "notes": notes,
                "verified": False,
                "needs_review": True,
            }

        _save(output_path, results)
        time.sleep(args.delay)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    found     = [n for n, v in results[discipline].items() if v.get("url") and v.get("verified")]
    unreachable = [n for n, v in results[discipline].items() if v.get("url") and not v.get("verified")]
    not_found = [n for n, v in results[discipline].items() if not v.get("url")]

    print(f"  Verified:    {len(found)}")
    print(f"  Unreachable: {len(unreachable)}")
    print(f"  Not found:   {len(not_found)}")

    if unreachable or not_found:
        print("\n  Needs manual review:")
        for n in unreachable + not_found:
            entry = results[discipline][n]
            print(f"    - {n}")
            print(f"      url:   {entry['url']}")
            print(f"      notes: {entry['notes']}")

    print(f"\nResults saved to: {output_path}")


if __name__ == "__main__":
    main()
