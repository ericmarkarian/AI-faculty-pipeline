"""
Merge all pipeline runs and export to Excel + HTML.

Usage:
    python3 tools/export_results.py

Inputs:
    faculty_Pediatric_Cardiology (2).csv  — original 30-school run (957 records)
    faculty_output.json                    — second patch run (23 records, better Penn + Colorado URLs)

Output:
    exports/faculty_pediatric_cardiology.xlsx
    exports/faculty_pediatric_cardiology.html
"""

import argparse
import json
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
EXPORTS = ROOT / "exports"
EXPORTS.mkdir(exist_ok=True)

CSV_PATH  = ROOT / "faculty_Pediatric_Cardiology (2).csv"
JSON_PATH = ROOT / "faculty_output.json"

# Schools that were re-scraped with better URLs in the patch run.
# Drop these from the CSV and use patch data instead.
PATCH_SCHOOLS = {
    "University of Pennsylvania Perelman School of Medicine",
    "University of Colorado School of Medicine / Children's Hospital Colorado",
    "University of Michigan Medical School",
    "Dell Medical School, University of Texas at Austin / Dell Children's",
}

ASSOC_PROF_PATTERN = re.compile(r"Professor", re.IGNORECASE)
ASST_PROF_PATTERN  = re.compile(r"Assistant\s+Professor", re.IGNORECASE)


def classify_rank(title: str | None) -> str:
    if not title or pd.isna(title):
        return "No Title"
    if ASST_PROF_PATTERN.search(title):
        return "Assistant Professor"
    if ASSOC_PROF_PATTERN.search(title):
        return "Professor / Associate Professor"
    return "Clinical / Other"


def is_assoc_prof_plus(title: str | None) -> bool:
    if not title or pd.isna(title):
        return False
    if ASST_PROF_PATTERN.search(title):
        return False
    return bool(ASSOC_PROF_PATTERN.search(title))


def _load_csv() -> pd.DataFrame:
    df = pd.read_csv(CSV_PATH)
    df = df.rename(columns={
        "Name":            "full_name",
        "School":          "school",
        "Title":           "title",
        "H-Index":         "h_index",
        "NIH Grants":      "nih_grant_count",
        "NIH Grant Titles":"nih_grant_titles",
        "Leadership":      "leadership_roles",
        "Editorial":       "editorial_roles",
        "Societies":       "society_roles",
        "Profile URL":     "profile_url",
    })
    df = df.drop(columns=["State"], errors="ignore")
    # Remove schools that have better data in the patch run
    df = df[~df["school"].isin(PATCH_SCHOOLS)]
    return df


def _load_patch_json() -> pd.DataFrame:
    with open(JSON_PATH) as f:
        records = json.load(f)

    rows = []
    for r in records:
        rows.append({
            "full_name":       r.get("full_name"),
            "school":          r.get("school"),
            "title":           r.get("title"),
            "h_index":         r.get("h_index"),
            "nih_grant_count": r.get("nih_grant_count", 0),
            "nih_grant_titles":r.get("nih_grant_titles"),
            "leadership_roles":_join_list(r.get("leadership_roles")),
            "editorial_roles": _join_list(r.get("editorial_roles")),
            "society_roles":   _join_list(r.get("society_roles")),
            "profile_url":     r.get("profile_url"),
        })
    return pd.DataFrame(rows)


def _join_list(val) -> str | None:
    if isinstance(val, list):
        return "; ".join(str(v) for v in val) if val else None
    return val or None


def merge() -> pd.DataFrame:
    base  = _load_csv()
    patch = _load_patch_json()
    df    = pd.concat([base, patch], ignore_index=True)

    # Deduplicate: keep last occurrence (patch data wins for overlapping names)
    df = df.drop_duplicates(subset=["full_name", "school"], keep="last")

    df["rank"] = df["title"].apply(classify_rank)
    df["assoc_prof_plus"] = df["title"].apply(is_assoc_prof_plus)
    df["nih_grant_count"] = pd.to_numeric(df["nih_grant_count"], errors="coerce").fillna(0).astype(int)

    return df.sort_values(["school", "full_name"]).reset_index(drop=True)


def export_excel(df: pd.DataFrame, path: Path) -> None:
    filtered  = df[df["assoc_prof_plus"]].copy()
    no_title  = df[~df["assoc_prof_plus"] & (df["rank"].isin(["No Title", "Clinical / Other"]))].copy()

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        _write_summary_sheet(df, filtered, writer)
        _write_sheet(filtered, "Assoc Prof+ (All Schools)", writer)
        _write_sheet(no_title, "No Prof Title", writer)
        _write_sheet(df, "All Faculty", writer)
        for school in sorted(df["school"].unique()):
            school_df = df[df["school"] == school]
            # Excel sheet names: max 31 chars, no / \ ? * [ ] :
            sheet_name = re.sub(r"[/\\?\*\[\]:]", "-", school)[:31]
            _write_sheet(school_df, sheet_name, writer)

    print(f"  Excel → {path}")


def _write_summary_sheet(
    df: pd.DataFrame,
    filtered: pd.DataFrame,
    writer: pd.ExcelWriter,
) -> None:
    schools = sorted(df["school"].unique())
    rows = []
    for school in schools:
        s_all  = df[df["school"] == school]
        s_filt = filtered[filtered["school"] == school]
        rows.append({
            "School":               school,
            "Total Faculty":        len(s_all),
            "Assoc Prof+":          len(s_filt),
            "With H-Index":         int(s_all["h_index"].notna().sum()),
            "With NIH Grants":      int((s_all["nih_grant_count"] > 0).sum()),
            "Avg H-Index":          round(s_all["h_index"].dropna().mean(), 1) if s_all["h_index"].notna().any() else "",
            "Total NIH Grants":     int(s_all["nih_grant_count"].sum()),
            "With Leadership Role": int(s_all["leadership_roles"].notna().sum()),
            "With Editorial Role":  int(s_all["editorial_roles"].notna().sum()),
            "With Society Role":    int(s_all["society_roles"].notna().sum()),
        })

    totals = {
        "School":               "TOTAL",
        "Total Faculty":        len(df),
        "Assoc Prof+":          len(filtered),
        "With H-Index":         int(df["h_index"].notna().sum()),
        "With NIH Grants":      int((df["nih_grant_count"] > 0).sum()),
        "Avg H-Index":          round(df["h_index"].dropna().mean(), 1) if df["h_index"].notna().any() else "",
        "Total NIH Grants":     int(df["nih_grant_count"].sum()),
        "With Leadership Role": int(df["leadership_roles"].notna().sum()),
        "With Editorial Role":  int(df["editorial_roles"].notna().sum()),
        "With Society Role":    int(df["society_roles"].notna().sum()),
    }
    rows.append(totals)

    summary_df = pd.DataFrame(rows)
    summary_df.to_excel(writer, sheet_name="Summary", index=False)

    ws = writer.sheets["Summary"]
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = max(
            len(str(col[0].value or "")),
            max(len(str(c.value or "")) for c in col),
        ) + 3


def _write_sheet(df: pd.DataFrame, sheet_name: str, writer: pd.ExcelWriter) -> None:
    cols = [
        "full_name", "school", "title", "rank", "h_index",
        "nih_grant_count", "leadership_roles", "editorial_roles",
        "society_roles", "nih_grant_titles", "profile_url",
    ]
    out = df[[c for c in cols if c in df.columns]].copy()
    out = out.rename(columns={
        "full_name":       "Name",
        "school":          "School",
        "title":           "Title",
        "rank":            "Rank",
        "h_index":         "H-Index",
        "nih_grant_count": "NIH Grants",
        "leadership_roles":"Leadership",
        "editorial_roles": "Editorial",
        "society_roles":   "Societies",
        "nih_grant_titles":"NIH Grant Titles",
        "profile_url":     "Profile URL",
    })
    out.to_excel(writer, sheet_name=sheet_name, index=False)

    ws = writer.sheets[sheet_name]
    col_widths = {"Name": 28, "School": 45, "Title": 40, "Rank": 30, "H-Index": 10,
                  "NIH Grants": 12, "Leadership": 50, "Editorial": 50,
                  "Societies": 50, "NIH Grant Titles": 60, "Profile URL": 50}
    for col in ws.columns:
        header = col[0].value
        ws.column_dimensions[col[0].column_letter].width = col_widths.get(header, 20)


def export_html(df: pd.DataFrame, path: Path) -> None:
    prof_df     = df[df["assoc_prof_plus"]].copy()
    no_title_df = df[~df["assoc_prof_plus"] & df["rank"].isin(["No Title", "Clinical / Other"])].copy()
    all_schools = sorted(df["school"].unique())

    prof_rows     = _build_rows(prof_df)
    no_title_rows = _build_rows(no_title_df)

    school_opts = "".join(
        f'<option value="{_esc(s)}">{_esc(s)}</option>' for s in all_schools
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Faculty Intelligence — Pediatric Cardiology</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Spectral:ital,wght@0,400;0,600;1,400;1,600&family=Figtree:wght@400;500;600;700;800&display=swap" rel="stylesheet"/>
<style>
  :root {{
    --red:        oklch(50% 0.26 27);
    --red-hover:  oklch(44% 0.24 25);
    --red-dim:    oklch(60% 0.20 27);
    --red-bg:     oklch(97% 0.030 25);
    --red-bg2:    oklch(92% 0.075 25);
    --red-text:   oklch(38% 0.22 25);
    --bg:         oklch(98.5% 0.004 25);
    --surface:    oklch(97%   0.006 25);
    --surface2:   oklch(94%   0.012 25);
    --border:     oklch(86%   0.014 25);
    --border-sub: oklch(92%   0.008 25);
    --ink:        oklch(16%   0.022 25);
    --ink2:       oklch(42%   0.016 25);
    --ink3:       oklch(63%   0.010 25);
    --qg-bg: oklch(95% 0.055 145); --qg-bd: oklch(76% 0.12 145); --qg-tx: oklch(29% 0.14 145);
    --qy-bg: oklch(96% 0.065  82); --qy-bd: oklch(78% 0.14  82); --qy-tx: oklch(36% 0.14  65);
  }}
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Figtree', system-ui, sans-serif;
    background: var(--bg);
    color: var(--ink);
    -webkit-font-smoothing: antialiased;
  }}
  input, select, button {{ font-family: 'Figtree', system-ui, sans-serif; }}
  ::-webkit-scrollbar {{ width: 4px; height: 4px; }}
  ::-webkit-scrollbar-track {{ background: transparent; }}
  ::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 4px; }}

  /* Layout */
  .topbar {{
    background: #fff; border-bottom: 1px solid var(--border-sub);
    padding: 0 36px; position: sticky; top: 0; z-index: 100;
  }}
  .topbar-inner {{
    max-width: 1440px; margin: 0 auto; height: 48px;
    display: flex; align-items: center; justify-content: space-between;
  }}
  .topbar-title {{
    font-family: 'Spectral', serif; font-size: 18px; font-weight: 600;
    letter-spacing: -0.01em; color: var(--ink);
  }}
  .topbar-meta {{ font-size: 11px; color: var(--ink3); }}
  .body {{ max-width: 1440px; margin: 0 auto; padding: 28px 36px 56px; display: flex; gap: 24px; align-items: flex-start; }}

  /* Sidebar */
  .sidebar {{
    flex-shrink: 0; width: 220px; position: sticky; top: 72px;
    background: #fff; border: 1px solid var(--border); border-radius: 10px;
    padding: 18px 16px; display: flex; flex-direction: column; gap: 0;
  }}
  .slabel {{
    font-size: 9px; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase;
    color: var(--ink3); margin-bottom: 7px;
  }}
  .sdivider {{ height: 1px; background: var(--border-sub); margin: 14px 0; }}
  .sidebar select {{
    width: 100%; padding: 8px 10px; border: 1.5px solid var(--border); border-radius: 8px;
    font-size: 12px; color: var(--ink); background: #fff; outline: none;
    transition: border-color 0.15s, box-shadow 0.15s; cursor: pointer;
  }}
  .sidebar select:focus {{ border-color: var(--red); box-shadow: 0 0 0 3px var(--red-bg2); }}
  .sidebar input[type=text] {{
    width: 100%; padding: 8px 10px; border: 1.5px solid var(--border); border-radius: 8px;
    font-size: 12px; color: var(--ink); background: #fff; outline: none;
    transition: border-color 0.15s, box-shadow 0.15s;
  }}
  .sidebar input[type=text]:focus {{ border-color: var(--red); box-shadow: 0 0 0 3px var(--red-bg2); }}
  .sidebar input[type=number] {{
    width: 100%; padding: 7px 10px; border: 1.5px solid var(--border); border-radius: 8px;
    font-size: 12px; color: var(--ink); background: #fff; outline: none;
    transition: border-color 0.15s, box-shadow 0.15s;
  }}
  .sidebar input[type=number]:focus {{ border-color: var(--red); box-shadow: 0 0 0 3px var(--red-bg2); }}
  .stats-row {{ font-size: 11px; color: var(--ink3); margin-top: 10px; }}
  .stats-row strong {{ color: var(--ink); }}

  /* Tabs */
  .tabs {{
    display: flex; border-bottom: 1px solid var(--border-sub); margin-bottom: 16px;
  }}
  .tab-btn {{
    padding: 8px 16px; font-size: 12px; font-weight: 600; cursor: pointer;
    border: none; background: none; color: var(--ink3);
    border-bottom: 2px solid transparent; margin-bottom: -1px;
    transition: color 0.15s; white-space: nowrap;
  }}
  .tab-btn:hover {{ color: var(--red); }}
  .tab-btn.active {{ color: var(--red); border-bottom-color: var(--red); }}
  .tab-count {{
    display: inline-flex; align-items: center; justify-content: center;
    min-width: 20px; height: 18px; padding: 0 6px;
    background: var(--surface2); color: var(--ink3);
    border-radius: 9px; font-size: 10px; font-weight: 700; margin-left: 6px;
    transition: background 0.15s, color 0.15s;
  }}
  .tab-btn.active .tab-count {{ background: var(--red-bg2); color: var(--red-text); }}
  .tab-panel {{ display: none; }}
  .tab-panel.active {{ display: block; }}

  /* Main content */
  .main {{ flex: 1; min-width: 0; }}
  .panel-title {{
    font-family: 'Spectral', serif; font-size: 20px; font-weight: 600;
    color: var(--ink); margin-bottom: 14px;
  }}

  /* Table */
  .tbl-wrap {{
    background: #fff; border: 1px solid var(--border); border-radius: 10px; overflow: hidden;
  }}
  table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
  thead th {{
    padding: 10px 14px; text-align: left; font-size: 10px; font-weight: 700;
    letter-spacing: 0.09em; text-transform: uppercase; color: var(--ink3);
    cursor: pointer; white-space: nowrap; background: var(--surface);
    border-bottom: 1px solid var(--border); user-select: none;
    transition: color 0.12s;
  }}
  thead th:hover {{ color: var(--red); }}
  .sort-arrow {{ margin-left: 4px; }}
  tbody tr {{ border-bottom: 1px solid var(--border-sub); transition: background 0.1s; }}
  tbody tr:hover td {{ background: var(--red-bg) !important; }}
  tbody tr:hover .name-cell {{ color: var(--red) !important; text-decoration: underline; text-underline-offset: 3px; }}
  td {{ padding: 11px 14px; font-size: 13px; vertical-align: top; background: #fff; }}
  .name-cell {{ font-weight: 600; font-size: 13px; transition: color 0.1s; }}
  .school-cell {{ font-size: 11px; color: var(--ink2); line-height: 1.4; }}
  .title-cell {{ font-size: 12px; color: var(--ink2); line-height: 1.4; }}
  .hindex-cell {{ text-align: center; font-weight: 700; font-size: 14px; }}
  .hindex-cell.hi {{ color: var(--qg-tx); }}
  .hindex-cell.mid {{ color: var(--qy-tx); }}
  .hindex-cell.dash {{ color: var(--ink3); font-weight: 400; font-size: 13px; }}
  .nih-cell {{ text-align: center; }}
  .nih-val {{ font-weight: 700; font-size: 14px; color: var(--red); }}
  .nih-zero {{ color: var(--ink3); font-size: 13px; }}
  .roles-cell {{ font-size: 11px; color: var(--ink2); line-height: 1.5; }}
  .url-cell a {{
    font-size: 11px; color: var(--red); text-decoration: none; font-weight: 600;
    transition: opacity 0.15s;
  }}
  .url-cell a:hover {{ opacity: 0.7; text-decoration: underline; }}
  .rank-badge {{
    display: inline-flex; align-items: center; gap: 4px;
    padding: 2px 9px; border-radius: 20px; font-size: 10px; font-weight: 700;
    letter-spacing: 0.04em;
  }}
  .rank-full  {{ background: var(--qg-bg); border: 1px solid var(--qg-bd); color: var(--qg-tx); }}
  .rank-assoc {{ background: oklch(94% 0.04 240); border: 1px solid oklch(76% 0.10 240); color: oklch(30% 0.12 240); }}
  .rank-other {{ background: var(--qy-bg); border: 1px solid var(--qy-bd); color: var(--qy-tx); }}
  .rank-none  {{ background: var(--surface2); border: 1px solid var(--border); color: var(--ink3); }}
  .hidden {{ display: none !important; }}
</style>
</head>
<body>

<!-- Topbar -->
<div class="topbar">
  <div class="topbar-inner">
    <span class="topbar-title">Faculty Intelligence</span>
    <span class="topbar-meta" id="topbar-count">Pediatric Cardiology · {len(df)} faculty · {df["school"].nunique()} institutions</span>
  </div>
</div>

<div class="body">

  <!-- Sidebar filters -->
  <div class="sidebar">
    <div class="slabel">Search</div>
    <input type="text" id="search-global" placeholder="Name, school, title, roles…" oninput="applyFilters(activeTab)">

    <div class="sdivider"></div>
    <div class="slabel">Institution</div>
    <select id="school-global" onchange="applyFilters(activeTab)">
      <option value="">All institutions</option>
      {school_opts}
    </select>

    <div class="sdivider"></div>
    <div class="slabel">NIH Grants</div>
    <select id="nih-global" onchange="applyFilters(activeTab)">
      <option value="">Any</option>
      <option value="1">Has grants</option>
      <option value="0">No grants</option>
    </select>

    <div class="sdivider"></div>
    <div class="slabel">Min H-Index</div>
    <input type="number" id="minh-global" min="0" placeholder="e.g. 10" oninput="applyFilters(activeTab)">

    <div class="stats-row" id="stats-sidebar">—</div>
  </div>

  <!-- Main -->
  <div class="main">
    <div class="tabs">
      <button class="tab-btn active" onclick="switchTab('prof', this)">
        Associate Professor+<span class="tab-count">{len(prof_df)}</span>
      </button>
      <button class="tab-btn" onclick="switchTab('notitle', this)">
        No Professor Title<span class="tab-count">{len(no_title_df)}</span>
      </button>
    </div>

    <!-- Tab: Associate Professor+ -->
    <div id="tab-prof" class="tab-panel active">
      <div class="panel-title">Associate Professor+</div>
      <div class="tbl-wrap">
        <table>
          <colgroup>
            <col style="width:17%"/> <!-- Name -->
            <col style="width:20%"/> <!-- Institution -->
            <col style="width:18%"/> <!-- Title -->
            <col style="width:12%"/> <!-- Rank -->
            <col style="width:7%" /> <!-- H -->
            <col style="width:7%" /> <!-- NIH -->
            <col style="width:16%"/> <!-- Roles -->
            <col style="width:3%" /> <!-- Link -->
          </colgroup>
          <thead><tr>
            <th onclick="sortTable('prof',0)">Name <span class="sort-arrow" id="sa-prof-0">↕</span></th>
            <th onclick="sortTable('prof',1)">Institution <span class="sort-arrow" id="sa-prof-1">↕</span></th>
            <th onclick="sortTable('prof',2)">Title <span class="sort-arrow" id="sa-prof-2">↕</span></th>
            <th onclick="sortTable('prof',3)">Rank <span class="sort-arrow" id="sa-prof-3">↕</span></th>
            <th onclick="sortTable('prof',4)">H-Index <span class="sort-arrow" id="sa-prof-4">↕</span></th>
            <th onclick="sortTable('prof',5)">NIH <span class="sort-arrow" id="sa-prof-5">↕</span></th>
            <th>Roles</th>
            <th></th>
          </tr></thead>
          <tbody id="tbody-prof">
{prof_rows}
          </tbody>
        </table>
      </div>
    </div>

    <!-- Tab: No Professor Title -->
    <div id="tab-notitle" class="tab-panel">
      <div class="panel-title">No Professor Title</div>
      <div class="tbl-wrap">
        <table>
          <colgroup>
            <col style="width:17%"/>
            <col style="width:20%"/>
            <col style="width:22%"/>
            <col style="width:10%"/>
            <col style="width:7%" />
            <col style="width:7%" />
            <col style="width:14%"/>
            <col style="width:3%" />
          </colgroup>
          <thead><tr>
            <th onclick="sortTable('notitle',0)">Name <span class="sort-arrow" id="sa-notitle-0">↕</span></th>
            <th onclick="sortTable('notitle',1)">Institution <span class="sort-arrow" id="sa-notitle-1">↕</span></th>
            <th onclick="sortTable('notitle',2)">Title <span class="sort-arrow" id="sa-notitle-2">↕</span></th>
            <th onclick="sortTable('notitle',3)">Rank <span class="sort-arrow" id="sa-notitle-3">↕</span></th>
            <th onclick="sortTable('notitle',4)">H-Index <span class="sort-arrow" id="sa-notitle-4">↕</span></th>
            <th onclick="sortTable('notitle',5)">NIH <span class="sort-arrow" id="sa-notitle-5">↕</span></th>
            <th>Roles</th>
            <th></th>
          </tr></thead>
          <tbody id="tbody-notitle">
{no_title_rows}
          </tbody>
        </table>
      </div>
    </div>

  </div><!-- /main -->
</div><!-- /body -->

<script>
let activeTab = 'prof';
const sortState = {{ prof: {{ col: -1, asc: true }}, notitle: {{ col: -1, asc: true }} }};

function switchTab(tab, btn) {{
  activeTab = tab;
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  btn.classList.add('active');
  applyFilters(tab);
}}

function applyFilters(tab) {{
  const q      = (document.getElementById('search-global')?.value || '').toLowerCase();
  const school = document.getElementById('school-global')?.value || '';
  const nih    = document.getElementById('nih-global')?.value || '';
  const minh   = parseFloat(document.getElementById('minh-global')?.value || '') || 0;
  let visible  = 0;

  document.querySelectorAll('#tbody-' + tab + ' tr').forEach(row => {{
    const h = parseFloat(row.dataset.hval || '');
    const ok = (!q      || row.dataset.text.includes(q))
            && (!school || row.dataset.school === school)
            && (!nih    || row.dataset.nih    === nih)
            && (!minh   || (!isNaN(h) && h >= minh));
    row.classList.toggle('hidden', !ok);
    if (ok) visible++;
  }});

  document.getElementById('stats-sidebar').innerHTML =
    `Showing <strong>${{visible}}</strong> faculty`;
}}

function sortTable(tab, col) {{
  const tbody = document.getElementById('tbody-' + tab);
  const rows  = Array.from(tbody.querySelectorAll('tr'));
  const state = sortState[tab];
  const asc   = state.col === col ? !state.asc : true;
  state.col = col; state.asc = asc;

  // Reset all sort arrows for this tab
  for (let i = 0; i < 6; i++) {{
    const el = document.getElementById('sa-' + tab + '-' + i);
    if (el) {{ el.textContent = '↕'; el.style.color = ''; }}
  }}
  const activeArrow = document.getElementById('sa-' + tab + '-' + col);
  if (activeArrow) {{
    activeArrow.textContent = asc ? '↑' : '↓';
    activeArrow.style.color = 'oklch(50% 0.26 27)';
  }}

  rows.sort((a, b) => {{
    const cellA = a.cells[col];
    const cellB = b.cells[col];
    if (col === 4) {{
      const ha = parseFloat(cellA?.dataset.hval ?? '');
      const hb = parseFloat(cellB?.dataset.hval ?? '');
      const av = !isNaN(ha), bv = !isNaN(hb);
      if (!av && !bv) return 0;
      if (!av) return 1;
      if (!bv) return -1;
      return asc ? ha - hb : hb - ha;
    }}
    const av = cellA?.innerText.trim() || '';
    const bv = cellB?.innerText.trim() || '';
    const na = parseFloat(av), nb = parseFloat(bv);
    if (!isNaN(na) && !isNaN(nb)) return asc ? na - nb : nb - na;
    return asc ? av.localeCompare(bv) : bv.localeCompare(av);
  }});
  rows.forEach(r => tbody.appendChild(r));
  applyFilters(tab);
}}

applyFilters('prof');
applyFilters('notitle');
</script>
</body>
</html>"""

    path.write_text(html, encoding="utf-8")
    print(f"  HTML  → {path}")


def _build_rows(df: pd.DataFrame) -> str:
    parts = []
    for _, row in df.iterrows():
        name   = _esc(_str(row.get("full_name")))
        school = _esc(_str(row.get("school")))
        title  = _esc(_str(row.get("title")))
        rank   = row.get("rank") or ""
        h      = row.get("h_index")
        nih    = int(row.get("nih_grant_count") or 0)
        lead   = _esc(_str(row.get("leadership_roles")))
        ed     = _esc(_str(row.get("editorial_roles")))
        soc    = _esc(_str(row.get("society_roles")))
        url    = _str(row.get("profile_url"))

        h_valid = pd.notna(h) and h
        h_num   = int(h) if h_valid else None
        h_val   = str(h_num) if h_num is not None else ""
        if h_num is None:
            h_disp = '<span style="color:var(--ink3)">—</span>'
            h_cls  = "hindex-cell dash"
        elif h_num >= 30:
            h_disp = str(h_num)
            h_cls  = "hindex-cell hi"
        elif h_num >= 15:
            h_disp = str(h_num)
            h_cls  = "hindex-cell mid"
        else:
            h_disp = str(h_num)
            h_cls  = "hindex-cell"

        nih_data = "1" if nih > 0 else "0"
        if nih > 0:
            nih_disp = f'<span class="nih-val">{nih}</span>'
        else:
            nih_disp = '<span class="nih-zero">—</span>'

        if "Professor" in rank and "Associate" not in rank and "Assistant" not in rank:
            rank_cls  = "rank-badge rank-full"
            rank_dot  = "●"
        elif "Associate" in rank or "Professor" in rank:
            rank_cls  = "rank-badge rank-assoc"
            rank_dot  = "●"
        elif rank == "No Title":
            rank_cls  = "rank-badge rank-none"
            rank_dot  = ""
        else:
            rank_cls  = "rank-badge rank-other"
            rank_dot  = ""
        rank_label = rank.replace("Professor / Associate Professor", "Prof / Assoc Prof")
        rank_html  = (
            f'<span class="{rank_cls}">'
            f'{"<span style=\'width:5px;height:5px;border-radius:50%;background:currentColor;flex-shrink:0;display:inline-block\'></span>" if rank_dot else ""}'
            f'{_esc(rank_label)}</span>'
        ) if rank else ""

        roles_parts = [p for p in [lead, ed, soc] if p]
        roles_disp  = "; ".join(roles_parts) if roles_parts else '<span style="color:var(--ink3)">—</span>'

        url_html = f'<a href="{_esc(url)}" target="_blank">↗</a>' if url else ""

        search_text = " ".join([name, school, title, rank, lead, ed, soc]).lower()

        parts.append(
            f'          <tr data-text="{_esc(search_text)}" data-school="{_esc(school)}" '
            f'data-rank="{_esc(rank)}" data-nih="{nih_data}" data-hval="{h_val}">'
            f'<td class="name-cell">{name}</td>'
            f'<td class="school-cell">{school}</td>'
            f'<td class="title-cell">{title or "<span style=\'color:var(--ink3)\'>—</span>"}</td>'
            f'<td>{rank_html}</td>'
            f'<td class="{h_cls}" data-hval="{h_val}">{h_disp}</td>'
            f'<td class="nih-cell">{nih_disp}</td>'
            f'<td class="roles-cell">{roles_disp}</td>'
            f'<td class="url-cell">{url_html}</td>'
            f'</tr>'
        )
    return "\n".join(parts)


def _str(val) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    return str(val)


def _esc(s: str) -> str:
    if not s:
        return ""
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


def load_from_excel(path: Path) -> pd.DataFrame:
    """Load the patched All Faculty sheet and restore internal column names."""
    df = pd.read_excel(path, sheet_name="All Faculty")
    df = df.rename(columns={
        "Name":            "full_name",
        "School":          "school",
        "Title":           "title",
        "Rank":            "rank",
        "H-Index":         "h_index",
        "NIH Grants":      "nih_grant_count",
        "Leadership":      "leadership_roles",
        "Editorial":       "editorial_roles",
        "Societies":       "society_roles",
        "NIH Grant Titles":"nih_grant_titles",
        "Profile URL":     "profile_url",
    })
    df["nih_grant_count"] = pd.to_numeric(df["nih_grant_count"], errors="coerce").fillna(0).astype(int)
    # Recompute rank/assoc_prof_plus from title so they're consistent
    df["rank"]           = df["title"].apply(classify_rank)
    df["assoc_prof_plus"] = df["title"].apply(is_assoc_prof_plus)
    return df


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--regen-html", action="store_true",
                        help="Regenerate HTML from the patched Excel (skip CSV/JSON merge)")
    args = parser.parse_args()

    xl_path   = EXPORTS / "faculty_pediatric_cardiology.xlsx"
    html_path = EXPORTS / "faculty_pediatric_cardiology.html"

    if args.regen_html:
        print("Loading from patched Excel…")
        df = load_from_excel(xl_path)
    else:
        print("Loading data…")
        df = merge()

    print(f"  Total faculty:        {len(df)}")
    print(f"  Associate Professor+: {df['assoc_prof_plus'].sum()}")
    print(f"  Schools:              {df['school'].nunique()}")
    print()

    print("Exporting…")
    if args.regen_html:
        export_html(df, html_path)
    else:
        export_excel(df, xl_path)
        export_html(df, html_path)
    print("\nDone.")


if __name__ == "__main__":
    main()
