"""
Backfill missing H-index and NIH grants into the merged faculty dataset,
then update exports/faculty_pediatric_cardiology.xlsx in-place.

Usage:
    python3 tools/backfill_enrichment.py [--hindex] [--nih] [--dry-run]

Flags (default: both enabled):
    --hindex   Re-query Scopus for faculty where h_index is blank
    --nih      Re-query NIH for schools where total NIH grant count == 0
    --dry-run  Print what would change without writing anything

Reads from:
    exports/faculty_pediatric_cardiology.xlsx  (All Faculty sheet)
Writes to:
    exports/faculty_pediatric_cardiology.xlsx  (all matching sheets patched)
"""

import argparse
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from openpyxl import load_workbook

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

XLSX_PATH = ROOT / "exports" / "faculty_pediatric_cardiology.xlsx"

SCOPUS_KEY = os.getenv("SCOPUS_API_KEY") or os.getenv("ELSEVIER_API_KEY")
DISCIPLINE = "Pediatric Cardiology"


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hindex",  action="store_true", help="Backfill missing H-index values")
    p.add_argument("--nih",     action="store_true", help="Backfill NIH grants for zero-grant schools")
    p.add_argument("--dry-run", action="store_true", help="Print changes without writing")
    args = p.parse_args()
    if not args.hindex and not args.nih:
        args.hindex = True
        args.nih = True
    return args


def _load_all_faculty() -> pd.DataFrame:
    return pd.read_excel(XLSX_PATH, sheet_name="All Faculty")


def _zero_grant_schools(df: pd.DataFrame) -> set[str]:
    """Schools where every faculty member has 0 NIH grants."""
    counts = df.groupby("School")["NIH Grants"].sum()
    return set(counts[counts == 0].index.tolist())


def _missing_hindex(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["H-Index"].isna() | (df["H-Index"] == "")].copy()


# ── patch application ─────────────────────────────────────────────────────────

def _apply_patches(patches: list[dict], dry_run: bool) -> None:
    """
    patches: list of {name, school, h_index?, nih_grant_count?, nih_grant_titles?}
    Updates all sheets in the workbook that contain matching rows.
    """
    if not patches:
        print("[backfill] No patches to apply.")
        return

    if dry_run:
        print(f"[backfill] DRY RUN — would apply {len(patches)} patch(es):")
        for p in patches:
            print(f"  {p['name']} @ {p['school']}: {p}")
        return

    wb = load_workbook(XLSX_PATH)

    # Build a lookup: (name_lower, school_lower) → patch dict
    lookup: dict[tuple[str, str], dict] = {
        (p["name"].lower().strip(), p["school"].lower().strip()): p
        for p in patches
    }

    changed_total = 0

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        headers = [cell.value for cell in ws[1]]

        try:
            name_col  = headers.index("Name")  + 1
            school_col = headers.index("School") + 1
        except ValueError:
            continue  # Sheet has no Name/School columns (e.g. Summary)

        h_col   = (headers.index("H-Index")         + 1) if "H-Index"         in headers else None
        nih_col = (headers.index("NIH Grants")       + 1) if "NIH Grants"       in headers else None
        tit_col = (headers.index("NIH Grant Titles") + 1) if "NIH Grant Titles" in headers else None

        changed = 0
        for row in ws.iter_rows(min_row=2):
            name_val   = row[name_col - 1].value
            school_val = row[school_col - 1].value
            if not name_val or not school_val:
                continue
            key = (str(name_val).lower().strip(), str(school_val).lower().strip())
            patch = lookup.get(key)
            if not patch:
                continue

            if h_col and "h_index" in patch and patch["h_index"] is not None:
                row[h_col - 1].value = patch["h_index"]
                changed += 1

            if nih_col and "nih_grant_count" in patch:
                row[nih_col - 1].value = patch["nih_grant_count"]
                changed += 1

            if tit_col and "nih_grant_titles" in patch:
                row[tit_col - 1].value = patch["nih_grant_titles"]
                changed += 1

        if changed:
            print(f"  [backfill] Sheet '{sheet_name}': {changed} cell(s) updated")
            changed_total += changed

    wb.save(XLSX_PATH)
    print(f"[backfill] Saved {XLSX_PATH}  ({changed_total} total cell(s) updated)")


# ── H-index backfill ──────────────────────────────────────────────────────────

def backfill_hindex(df: pd.DataFrame, dry_run: bool) -> None:
    from providers.live import LiveHIndexProvider

    missing = _missing_hindex(df)
    print(f"[hindex] {len(missing)} faculty with missing H-index")

    if missing.empty:
        return

    if not SCOPUS_KEY:
        print("[hindex] SCOPUS_API_KEY not set — cannot backfill H-index")
        return

    provider = LiveHIndexProvider(scopus_api_key=SCOPUS_KEY, discipline=DISCIPLINE)
    patches: list[dict] = []

    for _, row in missing.iterrows():
        name   = str(row.get("Name", "")).strip()
        school = str(row.get("School", "")).strip()
        if not name or not school:
            continue

        print(f"  [hindex] Querying: {name} @ {school}")
        try:
            result = provider.get_hindex(name, school)
        except Exception as e:
            print(f"  [hindex] ERROR for {name}: {e}")
            continue

        h = result.get("h_index")
        if h is not None:
            print(f"  [hindex] → h={h}")
            patches.append({"name": name, "school": school, "h_index": h})
        else:
            print(f"  [hindex] → not found ({result.get('evidence_text','')})")

    _apply_patches(patches, dry_run)


# ── NIH backfill ──────────────────────────────────────────────────────────────

def backfill_nih(df: pd.DataFrame, dry_run: bool) -> None:
    from providers.live import LiveNihProvider

    zero_schools = _zero_grant_schools(df)
    if not zero_schools:
        print("[nih] No schools with 0 NIH grants — nothing to backfill")
        return

    print(f"[nih] Schools with 0 grants ({len(zero_schools)}): {sorted(zero_schools)}")

    subset = df[df["School"].isin(zero_schools)].copy()

    provider = LiveNihProvider()
    patches: list[dict] = []

    for _, row in subset.iterrows():
        name   = str(row.get("Name", "")).strip()
        school = str(row.get("School", "")).strip()
        if not name or not school:
            continue

        print(f"  [nih] Querying: {name} @ {school}")
        try:
            grants = provider.get_grants(name, school)
        except Exception as e:
            print(f"  [nih] ERROR for {name}: {e}")
            continue

        count = len(grants)
        titles = "; ".join(g.get("title", "") for g in grants if g.get("title")) if grants else ""
        if count > 0:
            print(f"  [nih] → {count} grant(s)")
        patches.append({
            "name": name,
            "school": school,
            "nih_grant_count": count,
            "nih_grant_titles": titles,
        })

        # Rate-limit courtesy pause
        time.sleep(0.5)

    _apply_patches(patches, dry_run)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = _parse_args()

    if not XLSX_PATH.exists():
        print(f"[backfill] ERROR: {XLSX_PATH} not found — run export_results.py first")
        sys.exit(1)

    df = _load_all_faculty()
    print(f"[backfill] Loaded {len(df)} faculty from All Faculty sheet")

    if args.hindex:
        backfill_hindex(df, args.dry_run)

    if args.nih:
        backfill_nih(df, args.dry_run)

    print("[backfill] Done.")


if __name__ == "__main__":
    main()
