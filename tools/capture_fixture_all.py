"""
Capture all fixtures for the golden set in one command (NETWORK-ALLOWED — run once).

Usage:
    python -m tools.capture_fixture_all

Requires:
    OPENAI_API_KEY  — for roles capture
    SCOPUS_API_KEY  — for h-index capture (optional; h-index stored as null if missing)
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from openai import OpenAI

from tools.capture_fixture_roles import capture as capture_roles
from tools.capture_fixture_nih import capture as capture_nih
from tools.capture_fixture_hindex import capture as capture_hindex

_TESTS_DIR = Path(__file__).parent.parent / "tests"
_FIXTURES_ROLES = _TESTS_DIR / "fixtures" / "roles"
_FIXTURES_NIH = _TESTS_DIR / "fixtures" / "nih"
_FIXTURES_HINDEX = _TESTS_DIR / "fixtures" / "hindex"


def _slug(name: str) -> str:
    return name.lower().replace(" ", "_").replace(".", "").replace(",", "")


def _load_golden_set() -> list[dict]:
    with open(_TESTS_DIR / "golden_set.yaml") as f:
        return yaml.safe_load(f)["faculty"]


def main() -> None:
    openai_key = os.environ.get("OPENAI_API_KEY")
    scopus_key = os.environ.get("SCOPUS_API_KEY")

    if not openai_key:
        print("ERROR: OPENAI_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    if not scopus_key:
        print("WARNING: SCOPUS_API_KEY not set — h-index will be stored as null.")

    client = OpenAI(api_key=openai_key)
    golden = _load_golden_set()

    print(f"Capturing fixtures for {len(golden)} faculty...\n")
    results = []

    for person in golden:
        name, school = person["name"], person["school"]
        s = _slug(name)
        print(f"{'─' * 50}")
        print(f"{name} ({school})")

        roles_fixture = capture_roles(name, school, _FIXTURES_ROLES / f"{s}.json", client)
        capture_nih(name, school, _FIXTURES_NIH / f"{s}.json")
        hindex_fixture = capture_hindex(name, school, _FIXTURES_HINDEX / f"{s}.json", scopus_key)

        results.append({
            "name": name,
            "roles": len(roles_fixture.get("roles", [])),
            "possible_miss": roles_fixture.get("possible_miss", False),
            "h_index": hindex_fixture.get("h_index"),
        })
        print()

    print("=" * 50)
    print("  CAPTURE SUMMARY")
    print("=" * 50)
    print(f"  {'Name':<22} {'Roles':>6} {'Miss':>5} {'H-index':>8}")
    print(f"  {'-'*22} {'-'*6} {'-'*5} {'-'*8}")
    for r in results:
        miss = "YES" if r["possible_miss"] else "no"
        h = str(r["h_index"]) if r["h_index"] is not None else "N/A"
        print(f"  {r['name']:<22} {r['roles']:>6} {miss:>5} {h:>8}")
    print("=" * 50)


if __name__ == "__main__":
    main()
