# AI Faculty Search Pipeline

A prototype AI agent pipeline to systematically identify and evaluate faculty candidates across medical institutions. Built at Stanford Clinical Informatics to reduce bias in faculty recruitment by automating candidate discovery beyond word-of-mouth networks.

## Overview

The pipeline runs three agents in sequence:

| Agent | Role | Tools |
|-------|------|-------|
| Agent 1 — Discovery | Finds all faculty at a given school and discipline | Playwright directory scrape + GPT-4o web search (both run, results unioned) |
| Agent 2 — Enrichment | Looks up H-index for each faculty member | Scopus API |
| Agent 3 — QC | Flags duplicates, conflicts, scores confidence | GPT-4o (disabled for now) |

Output is a `faculty_output.csv` and `faculty_output.json` ready for review.

## Requirements

```bash
pip install openai pandas requests
```

You will need two API keys:

- **OpenAI API key** — from [platform.openai.com](https://platform.openai.com)
- **Scopus API key** — from [dev.elsevier.com](https://dev.elsevier.com) (requires institutional subscription, e.g. Stanford)

> ⚠️ The Scopus API requires you to be on your institution's network or VPN.

## Setup

```bash
export OPENAI_API_KEY="your-openai-key"
export SCOPUS_API_KEY="your-scopus-key"
```

Never hardcode API keys in the script or commit them to the repo.

## Usage

```bash
python3 faculty_search.py
```

By default it searches for **Pediatric Endocrinology** faculty at **Stanford**. To change the target, edit these lines at the top of the script:

```python
SCHOOLS = ["Stanford"]
DISCIPLINE = "Pediatric Endocrinology"
```

To add more schools, add them to `SCHOOLS` and update `INSTITUTION_MAP` and `INSTITUTION_KEYWORDS` with the relevant affiliation info.

## Output

Results are saved to:
- `faculty_output.csv` — tabular, easy to open in Excel or Google Sheets
- `faculty_output.json` — full structured data

Fields currently populated:
| Field | Source |
|-------|--------|
| full_name | Agent 1 |
| title | Agent 1 |
| subspecialty | Agent 1 |
| email | Agent 1 |
| profile_url | Agent 1 |
| h_index | Agent 2 · Scopus API |

Fields ready to re-enable (currently commented out):
- `nih_grants` — NIH Reporter API
- `leadership_roles`, `editorial_roles`, `society_roles`, `training_roles` — GPT web search
- `confidence_score`, `duplicate_flag`, `qc_notes` — Agent 3 QC

## Known Limitations

- **H-index coverage** — Scopus only returns an H-index if the faculty member has 2+ publications indexed. Clinician-educators with limited publication records may return `null`.
- **Name disambiguation** — searches by first + last name only. Middle names are ignored. Common names may occasionally match the wrong person if no Stanford-affiliated result is found.
- **Affiliation matching** — filters Scopus results by checking if "stanford" appears in the returned affiliation name. Works for all known Stanford sub-institutions but may need adjustment for other schools.
- **Network requirement** — Scopus API requires Stanford VPN or on-campus WiFi.

## Golden Set Tests (5 people)

A zero-network test harness validates roles extraction quality and NIH grant enrichment against a fixed golden set. Tests run fully offline after a one-time fixture capture.

### Golden set

| Name | Archetype |
|------|-----------|
| David Maahs | high-signal (roles across all categories) |
| Anna Gloyn | editorial-heavy |
| Tandy Aye | society-heavy |
| Darrell Wilson | grant-heavy |
| Holly Cooper | low-signal (zero roles — hallucination guard) |

### Step 1: Capture fixtures (once, requires network)

```bash
pip install pytest pyyaml

# Capture roles fixtures
python -m tools.capture_fixture_roles --name "David Maahs"   --school "Stanford" --out tests/fixtures/roles/david_maahs.json
python -m tools.capture_fixture_roles --name "Anna Gloyn"    --school "Stanford" --out tests/fixtures/roles/anna_gloyn.json
python -m tools.capture_fixture_roles --name "Tandy Aye"     --school "Stanford" --out tests/fixtures/roles/tandy_aye.json
python -m tools.capture_fixture_roles --name "Darrell Wilson" --school "Stanford" --out tests/fixtures/roles/darrell_wilson.json
python -m tools.capture_fixture_roles --name "Holly Cooper"  --school "Stanford" --out tests/fixtures/roles/holly_cooper.json

# Capture NIH fixtures
python -m tools.capture_fixture_nih --name "Darrell Wilson" --school "Stanford" --out tests/fixtures/nih/darrell_wilson.json
```

### Step 2: Run tests offline (no network)

```bash
pytest tests/test_golden_set.py -v
```

Tests will fail immediately if any network call is attempted. The audit report prints at the end of each run.

### Snapshot regression

On first run, snapshots are written to `tests/snapshots/`. On subsequent runs, any change in extracted roles produces a readable unified diff so prompt/model regressions are visible.

To reset a snapshot after an intentional change:
```bash
rm tests/snapshots/<slug>.json
pytest tests/test_golden_set.py -v  # re-captures baseline
```

## Project Structure

```
faculty_search.py        # Main pipeline script
providers/
  base.py                # NihProvider + RolesProvider ABCs
  live.py                # Live network implementations
  replay.py              # Replay (fixture-based) implementations
tools/
  capture_fixture_roles.py  # CLI: capture roles fixture once
  capture_fixture_nih.py    # CLI: capture NIH fixture once
tests/
  golden_set.yaml        # 5 faculty with archetypes + thresholds
  conftest.py            # Network guard + shared loaders
  test_golden_set.py     # Schema, archetype, snapshot tests
  fixtures/roles/        # Captured roles fixtures (not committed)
  fixtures/nih/          # Captured NIH fixtures (not committed)
  snapshots/             # Regression baselines (commit these)
README.md
```

