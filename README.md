# ATLAS — AI Faculty Search Pipeline

**ATLAS** (**A**cademic **T**alent **L**ocation **A**nd **S**ourcing) is an AI agent pipeline that finds and profiles faculty across medical institutions. Give it a **discipline** and a set of **institutions**; it returns a structured, source-linked dataset of every matching faculty member — in minutes rather than hours. Built at Stanford Clinical Informatics.

## Why it exists

Faculty recruiting in academic medicine runs on informal, word-of-mouth networks. That systematically overlooks qualified candidates outside an institution's existing network, regardless of merit. The manual alternative is a recruiter spending hours per specialty hand-searching faculty directories, cross-referencing NIH databases, and verifying credentials one person at a time.

ATLAS replaces "who do we know" with a systematic sweep, then enriches each person with **objective, sourced metrics** — NIH grant history, h-index, named professional roles (editorial boards, society offices, institutional leadership, training-program directorships), email, and education.

### Who uses it, and for what

- **Search committees and recruiters** — build a complete candidate pool for an open position: "every Pediatric Endocrinology faculty member at these 12 institutions," with funding and leadership signals attached, rather than a shortlist of known names.
- **Division and department leadership** — survey a specialty's national landscape: who holds society and editorial roles, where NIH-funded investigators are concentrated.
- **Program and grant teams** — assemble reviewer pools, speaker lists, or collaborator shortlists filtered by concrete criteria (active NIH funding, training-program directorship).
- **Named-list enrichment** — already have candidates? Names mode skips discovery and enriches a hand-supplied list of name + institution pairs.

### Design principle: discovery, not ranking

ATLAS is intentionally **not** an evaluation or scoring system.

- **Objective metrics only** — verifiable data from named external databases. No composite "quality score."
- **Filter, don't rank** — users explore by criteria; the system never emits an ordered list implying relative merit.
- **Transparency** — every data point traces back to its source, and every claim carries a green / yellow / red confidence badge from an independent verification pass.
- **Bias reduction by design** — surfacing *all* matching faculty regardless of network connection is the point.

## How it works

Three agents run in sequence:

| Agent | Role | Sources |
|-------|------|---------|
| Agent 1 — Discovery | Finds all faculty at a given school + discipline | Playwright directory scrape **and** GPT-4o web search (both always run, results unioned and deduplicated) |
| Agent 2 — Enrichment | NIH grants, h-index, named roles, email, education | NIH Reporter · Scopus (OpenAlex fallback) · PubMed · profile pages |
| Agent 3 — QC | Re-verifies every claim, flags duplicates and conflicts, assigns confidence | GPT-4o |

Every record carries a `discovery_source` (`university`, `web_search`, `university+web_search`, …) so gaps between a batch sweep and a manual lookup are auditable. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for internals.

## Quickstart

```bash
pip install -r requirements.txt
playwright install chromium     # one-time browser download for the scraper
```

Set credentials in a `.env` file in the repo root (loaded automatically — never commit real keys):

```bash
OPENAI_API_KEY=sk-...
SCOPUS_API_KEY=...
```

### Run from the browser (recommended)

```bash
python3 server.py
# → [Server] Starting at http://localhost:5050
```

Two modes: a **discipline sweep** across selected institutions, and **names mode** for a hand-supplied list. Results stream in live over SSE — each card appears as the person is enriched, then updates again with its QC badge.

### Run from the command line

Targets are set at the top of [`faculty_search.py`](faculty_search.py) (`SCHOOLS`, `DISCIPLINE`, `MAX_WORKERS`):

```bash
python3 faculty_search.py
```

Or call `run_pipeline(schools=..., discipline=...)` / `run_pipeline_from_records(records, ...)` directly — see [docs/USAGE.md](docs/USAGE.md).

## Configuration

| Variable | Required? | Used for |
|----------|-----------|----------|
| `OPENAI_API_KEY` | **Yes** | Discovery, roles extraction, profile parsing, QC |
| `SCOPUS_API_KEY` | Recommended | Primary h-index source; falls back to OpenAlex without it |
| `SCOPUS_INSTTOKEN` | Optional | Scopus institutional token (off-network access) |
| `NCBI_API_KEY` / `PUBMED_API_KEY` | Optional | Higher PubMed rate limit (email fallback lookups) |
| `OPENALEX_API_KEY` | Optional | Higher-concurrency OpenAlex pool |
| `AGENT1_WEB_SEARCH` | Optional | `0` runs Agent 1 from the scraped directory pages only. Default `1` — directory scrape **and** web search, unioned |
| `SITE_PASSWORD` | Optional | If set, the web UI requires this password (HTTP basic auth) |
| `PORT` | Optional | Web server port (default `5050`) |

> **Scopus note:** the Scopus API generally requires your institution's network or VPN (or a valid `SCOPUS_INSTTOKEN`). If Scopus is unavailable the pipeline falls back to OpenAlex for h-index — it does not fail.

Full variable reference, deployment notes, and troubleshooting: [docs/USAGE.md](docs/USAGE.md).

## Output

- `faculty_output.csv` — tabular, with flattened QC columns, easy to open in Excel or Sheets
- `faculty_output.json` — full structured data, including the nested `qc` object
- `faculty_summaries.txt` — plain-language paragraphs, templated from collected data (no extra LLM call)
- **Live UI** — the same records streamed over SSE as they complete

Columns: `full_name`, `title`, `profile_url`, `school`, `subspecialty`, `email`, `education`, `h_index`, `h_index_source`, `nih_project_nums`, `nih_grant_titles`, `nih_grant_count`, `nih_institution_unconfirmed`, `editorial_roles`, `society_roles`, `training_roles`, `leadership_roles`, `summary`, `qc`, `qc_nih`, `qc_hindex`, `qc_roles`.

## Documentation

| Doc | Contents |
|-----|----------|
| [docs/OVERVIEW.md](docs/OVERVIEW.md) | What ATLAS is, the problem it solves, design principles |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Per-agent internals, provider abstraction, QC checks, observability |
| [docs/USAGE.md](docs/USAGE.md) | Setup, both run modes, adding institutions/specialties, troubleshooting |
| [docs/PRD.md](docs/PRD.md) | Product requirements and scope |

## Golden Set Tests (5 people)

A zero-network test harness validates roles extraction quality and NIH grant enrichment against a fixed golden set. Tests run fully offline after a one-time fixture capture.

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
pytest tests/test_golden_set.py -v    # golden set only
pytest                                # full suite
```

Tests fail immediately if any network call is attempted. The audit report prints at the end of each run.

### Snapshot regression

On first run, snapshots are written to `tests/snapshots/`. On subsequent runs, any change in extracted roles produces a readable unified diff so prompt/model regressions are visible.

To reset a snapshot after an intentional change:
```bash
rm tests/snapshots/<slug>.json
pytest tests/test_golden_set.py -v  # re-captures baseline
```

## Known Limitations

- **H-index coverage** — Scopus only returns an h-index if the faculty member has 2+ indexed publications. Clinician-educators with limited publication records may return `null`.
- **Name disambiguation** — matching uses first + last name with institution gating. Two faculty at one school sharing a first and last name will merge.
- **Web-search trust** — web-search discoveries are model-reported rather than page-grounded, so they can include people who have moved or are miscategorized. They enter at the lowest merge precedence and are tagged in `discovery_source`.
- **Network requirement** — Scopus generally requires institutional network, VPN, or an institutional token.

## Project Structure

```
faculty_search.py                 # Pipeline: Agents 1-3, CSV/JSON output
server.py                         # Flask app — SSE streaming, sweep + names modes, demo replay
observe.py                        # Structured logging, cost tracking, alerting
medfaculty.html                   # Web UI (React, served by server.py)
frontend/                         # Standalone static results page + UI screenshots
providers/
  base.py                         # ScraperProvider / NihProvider / RolesProvider / HIndexProvider ABCs
  generic_faculty_scraper.py      # Agent 1: directory scrape + web search, unioned
  playwright_scraper.py           # Deterministic HTML directory parsing
  faculty_merge.py                # Name normalization, cross-source merge, dedupe
  url_discovery.py                # Finds a school's faculty directory URL
  live.py                         # Live network implementations (NIH, Scopus, OpenAlex, roles)
  replay.py                       # Fixture-based replays for offline tests
tools/
  capture_fixture_roles.py        # CLI: capture roles fixture once
  capture_fixture_nih.py          # CLI: capture NIH fixture once
  discover_faculty_urls.py        # CLI: bulk directory-URL discovery
tests/                            # Offline suite: golden set, merge, scraper sources
docs/                             # OVERVIEW · ARCHITECTURE · USAGE · PRD
Procfile, render.yaml             # Deployment (gunicorn on Render)
```
