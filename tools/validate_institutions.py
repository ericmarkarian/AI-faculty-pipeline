"""
Validate NIH org-name resolution for every school configured in faculty_urls.json.

Usage:
    python3 tools/validate_institutions.py

Output: a table showing resolved NIH names and Scopus aliases per school,
flagging any that fell through to the last-resort fallback.
"""
import json
import sys
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from providers.institution_resolver import (
    get_nih_org_names,
    get_scopus_aliases,
    _generate_nih_candidates,
    _nih_fallback,
)
from providers.institutions import INSTITUTION_NIH_NAMES

_URLS_PATH = Path(__file__).parent.parent / "data" / "faculty_urls.json"


def main() -> None:
    data = json.loads(_URLS_PATH.read_text())

    # Collect all unique school names across all disciplines
    schools: set[str] = set()
    for disc_map in data.values():
        schools.update(disc_map.keys())

    print(f"Validating {len(schools)} schools...\n")
    print(f"{'School':<65} {'NIH names':<45} {'Scopus aliases'}")
    print("-" * 140)

    warnings: list[str] = []

    for school in sorted(schools):
        nih = get_nih_org_names(school)
        scopus = get_scopus_aliases(school)

        # Flag schools that fell to the raw-name fallback
        fallback = _nih_fallback(school)
        is_fallback = nih == fallback and school not in INSTITUTION_NIH_NAMES

        nih_str    = ", ".join(nih[:2]) + ("…" if len(nih) > 2 else "")
        scopus_str = ", ".join(scopus[:2]) + ("…" if len(scopus) > 2 else "")
        flag = " ⚠ FALLBACK" if is_fallback else ""

        print(f"{school:<65} {nih_str:<45} {scopus_str}{flag}")

        if is_fallback:
            warnings.append(school)

    if warnings:
        print(f"\n⚠  {len(warnings)} school(s) using fallback NIH name — grants may return 0 results:")
        for w in warnings:
            cands = _generate_nih_candidates(w)
            print(f"   {w}")
            print(f"     Candidates tried: {cands}")
    else:
        print("\n✓ All schools resolved successfully.")


if __name__ == "__main__":
    main()
