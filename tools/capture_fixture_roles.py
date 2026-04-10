"""
Capture roles fixture for one faculty member (NETWORK-ALLOWED — run once).

Adds URL verification and miss detection to each fixture.

Usage:
    python -m tools.capture_fixture_roles \
        --name "David Maahs" --school "Stanford" \
        --out tests/fixtures/roles/david_maahs.json
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
from openai import OpenAI

from providers.live import LiveRolesProvider, _parse_json, _LIVE_MODEL

_MISS_ALTERNATE_PROMPTS: dict[str, list[str]] = {
    "editorial": [
        'Search the web for journal editorial board memberships held by {name} at {school}. '
        'Include only named editor or editorial board member roles, not general peer review. '
        'Return ONLY JSON: {{"roles": [{{"role": "...", "org": "...", "evidence": "..."}}]}} or {{"roles": []}}.',
        'Search on journal and publisher websites for {name} listed as an editor or editorial board member. '
        'Return ONLY JSON: {{"roles": [{{"role": "...", "org": "...", "evidence": "..."}}]}} or {{"roles": []}}.',
    ],
    "society": [
        'Search for professional medical society committee or officer roles held by {name} at {school}. '
        'Exclude general membership — only named committee or leadership positions. '
        'Return ONLY JSON: {{"roles": [{{"role": "...", "org": "...", "evidence": "..."}}]}} or {{"roles": []}}.',
        'Search on medical society websites for {name} listed as a committee chair, officer, or board member. '
        'Return ONLY JSON: {{"roles": [{{"role": "...", "org": "...", "evidence": "..."}}]}} or {{"roles": []}}.',
    ],
    "leadership": [
        'Search for divisional or program leadership roles held by {name} at {school}. '
        'Include division chief, program director, center director, department chair. '
        'Return ONLY JSON: {{"roles": [{{"role": "...", "org": "...", "evidence": "..."}}]}} or {{"roles": []}}.',
        'Search on Stanford Medicine or hospital websites for {name} in a director, chief, or chair role. '
        'Return ONLY JSON: {{"roles": [{{"role": "...", "org": "...", "evidence": "..."}}]}} or {{"roles": []}}.',
    ],
    "training": [
        'Search for training or education program director roles held by {name} at {school}. '
        'Include fellowship director, clerkship director, residency program director. '
        'Return ONLY JSON: {{"roles": [{{"role": "...", "org": "...", "evidence": "..."}}]}} or {{"roles": []}}.',
        'Search on medical education websites for {name} listed as a fellowship or training program director. '
        'Return ONLY JSON: {{"roles": [{{"role": "...", "org": "...", "evidence": "..."}}]}} or {{"roles": []}}.',
    ],
}


def _verify_url(source_url: str, evidence_text: str) -> dict:
    try:
        resp = requests.get(
            source_url, timeout=10, allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ResearchValidator/1.0)"},
        )
        page_text = resp.text.lower()
        evidence_lower = evidence_text.lower()

        if evidence_lower in page_text:
            method, found = "exact", True
        else:
            tokens = [t for t in evidence_lower.split() if len(t) > 3]
            if tokens:
                matched = sum(1 for t in tokens if t in page_text)
                found = matched / len(tokens) >= 0.5
                method = "fuzzy" if found else "not_found"
            else:
                found, method = False, "not_found"

        return {
            "http_status": resp.status_code,
            "final_url": resp.url,
            "content_hash": hashlib.sha256(resp.content).hexdigest(),
            "evidence_found": found,
            "evidence_match_method": method,
        }
    except Exception as e:
        return {
            "http_status": None,
            "final_url": source_url,
            "content_hash": None,
            "evidence_found": False,
            "evidence_match_method": "error",
            "error": str(e),
        }


def _run_alternate_query(prompt: str, client) -> bool:
    """Returns True if the alternate query found any role evidence."""
    try:
        response = client.responses.create(
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
        return len(parsed.get("roles", [])) > 0
    except Exception:
        return False


def _detect_misses(name: str, school: str, primary_roles: list[dict], client) -> tuple[bool, dict]:
    """Run alternate queries for empty categories. Returns (possible_miss, miss_detection)."""
    by_category = {
        cat: [r for r in primary_roles if r.get("category") == cat]
        for cat in ["editorial", "society", "leadership", "training"]
    }

    miss_detection: dict = {}
    overall_possible_miss = False

    for category, primary in by_category.items():
        if primary:
            miss_detection[category] = {
                "checked": False,
                "primary_count": len(primary),
                "alternate_found": 0,
                "possible_miss": False,
            }
            continue

        print(f"    [miss] {category} empty — running alternate queries...")
        alternate_found = 0
        for prompt_template in _MISS_ALTERNATE_PROMPTS[category][:2]:
            prompt = prompt_template.format(name=name, school=school)
            if _run_alternate_query(prompt, client):
                alternate_found += 1

        # require consensus from both queries to reduce false positives
        cat_possible_miss = alternate_found >= 2
        if cat_possible_miss:
            overall_possible_miss = True
            print(f"    [miss] WARNING: alternate found evidence for {category}!")

        miss_detection[category] = {
            "checked": True,
            "primary_count": 0,
            "alternate_found": alternate_found,
            "possible_miss": cat_possible_miss,
        }

    return overall_possible_miss, miss_detection


def capture(
    name: str,
    school: str,
    out_path: Path,
    client,
    profile_url: str = "",
) -> dict:
    """Capture roles fixture with URL verification and miss detection. Returns the fixture dict."""
    print(f"  [roles] Fetching roles for {name} ({school})...")
    provider = LiveRolesProvider(client)
    roles = provider.get_roles(name, school, profile_url)

    print(f"  [roles] Verifying {len(roles)} source URLs...")
    for role in roles:
        role["url_verification"] = _verify_url(
            role.get("source_url", ""), role.get("evidence_text", "")
        )
        time.sleep(0.5)

    print(f"  [roles] Running miss detection...")
    possible_miss, miss_detection = _detect_misses(name, school, roles, client)

    fixture = {
        "name": name,
        "school": school,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "possible_miss": possible_miss,
        "miss_detection": miss_detection,
        "roles": roles,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(fixture, indent=2))

    by_cat: dict[str, int] = {}
    for r in roles:
        cat = r.get("category", "unknown")
        by_cat[cat] = by_cat.get(cat, 0) + 1
    print(f"  [roles] {len(roles)} roles -> {out_path}")
    for cat, count in sorted(by_cat.items()):
        print(f"           {cat}: {count}")
    if possible_miss:
        flagged = [c for c, d in miss_detection.items() if d.get("possible_miss")]
        print(f"  [roles] WARNING possible_miss=True for: {flagged}")

    return fixture


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture roles fixture (live network call).")
    parser.add_argument("--name", required=True)
    parser.add_argument("--school", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--profile-url", default="")
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    capture(args.name, args.school, Path(args.out), OpenAI(api_key=api_key), args.profile_url)


if __name__ == "__main__":
    main()
