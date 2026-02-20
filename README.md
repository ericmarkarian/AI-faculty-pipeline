# AI Faculty Search Pipeline

A prototype AI agent pipeline to systematically identify and evaluate faculty candidates across medical institutions. Built at Stanford Clinical Informatics to reduce bias in faculty recruitment by automating candidate discovery beyond word-of-mouth networks.

## Overview

The pipeline runs three agents in sequence:

| Agent | Role | Tools |
|-------|------|-------|
| Agent 1 — Discovery | Finds all faculty at a given school and discipline | GPT-4o with live web search |
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

## Project Structure

```
faculty_search.py   # Main pipeline script
get_afids.py        # Utility script for exploring Scopus affiliation IDs
README.md
.gitignore
```

## Team

Built by Eric Markarian as part of the Stanford Clinical Informatics fellowship under Dr. Natalie Pageler.
