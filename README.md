# ATLAS — Equitable Faculty Discovery for Academic Medicine

**ATLAS** (**A**cademic **T**alent **L**ocation **A**nd **S**ourcing) is an open-source tool built to make faculty recruitment in academic medicine more equitable. Faculty searches today run on informal, word-of-mouth networks — "who do we know?" — which systematically overlooks qualified candidates outside a search committee's existing circles, regardless of merit. ATLAS replaces that with a systematic sweep: give it a **discipline** and a set of **institutions**, and it returns a structured, source-linked profile of *every* matching faculty member — in minutes rather than the hours of hand-searching directories, cross-referencing NIH databases, and verifying credentials one person at a time.

Built at Stanford Clinical Informatics. Free to use, modify, and deploy under the [MIT License](LICENSE).

## The equity problem ATLAS addresses

Who gets recruited for faculty positions, invited to speak, or tapped for leadership in academic medicine depends heavily on who is already visible to the people searching. Informal networks reproduce themselves: candidates at less-connected institutions, from underrepresented backgrounds, or simply outside a committee's orbit never make the list — not because they lack qualifications, but because nobody looked.

ATLAS makes the looking systematic:

- **Complete candidate pools, not shortlists of known names** — every faculty member in the discipline at the institutions searched, with the same data collected for each.
- **Objective, sourced metrics** — NIH grant history, h-index, named professional roles (editorial boards, society offices, institutional leadership, training-program directorships), education, and contact information — each traceable to a named external source.
- **Filter, don't rank** — ATLAS is intentionally **not** a scoring or evaluation system. Users explore by concrete criteria; the system never emits an ordered list implying relative merit, and no composite "quality score" exists anywhere in it.
- **Transparency and verification** — every claim carries a green / yellow / red confidence badge from an independent QC pass, so human judgment stays informed rather than replaced.

### Who it's for

- **Search committees and recruiters** — build the full candidate pool for an open position: "every Pediatric Endocrinology faculty member at these 12 institutions," with funding and leadership signals attached.
- **Division and department leadership** — survey a specialty's national landscape: who holds society and editorial roles, where NIH-funded investigators are concentrated.
- **Program and grant teams** — assemble reviewer pools, speaker lists, or collaborator shortlists filtered by concrete criteria.
- **Named-list enrichment** — already have candidates? Names mode skips discovery and enriches a hand-supplied list of name + institution pairs.

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
python3 server.py
# → [Server] Starting at http://localhost:5050
```

On first open, ATLAS asks for your **OpenAI API key** in the app (create one at [platform.openai.com/api-keys](https://platform.openai.com/api-keys)). The key is verified, saved to a local `.env` on your machine, and never leaves it except to call OpenAI — you won't be asked again. Bring-your-own-key means anyone can run ATLAS on their own OpenAI account; no shared credentials involved.

Optional keys (Scopus, PubMed, OpenAlex — see Configuration) can be added to the same `.env` file by hand.

Two modes are available in the browser: a **discipline sweep** across selected institutions, and **names mode** for a hand-supplied list. Results stream in live — each card appears as the person is enriched, then updates with its QC badge.

### Run from the command line

Targets are set at the top of [`faculty_search.py`](faculty_search.py) (`SCHOOLS`, `DISCIPLINE`, `MAX_WORKERS`):

```bash
python3 faculty_search.py
```

Or call `run_pipeline(schools=..., discipline=...)` / `run_pipeline_from_records(records, ...)` directly — see [docs/USAGE.md](docs/USAGE.md).

## Configuration

| Variable | Required? | Used for |
|----------|-----------|----------|
| `OPENAI_API_KEY` | **Yes** — the app prompts for it on first open | Discovery, roles extraction, profile parsing, QC |
| `SCOPUS_API_KEY` | Recommended | Primary h-index source; falls back to OpenAlex without it |
| `SCOPUS_INSTTOKEN` | Optional | Scopus institutional token (off-network access) |
| `NCBI_API_KEY` / `PUBMED_API_KEY` | Optional | Higher PubMed rate limit (email fallback lookups) |
| `OPENALEX_API_KEY` | Optional | Higher-concurrency OpenAlex pool |
| `AGENT1_WEB_SEARCH` | Optional | `0` runs Agent 1 from the scraped directory pages only. Default `1` — directory scrape **and** web search, unioned |
| `SITE_PASSWORD` | Optional | If set, the web UI requires this password (HTTP basic auth) |
| `PORT` | Optional | Web server port (default `5050`) |

> **Shared deployments:** on a hosted instance (e.g. Render, which sets `RENDER=true`), a key entered in the app is kept in the visitor's own browser and sent per run — it is never stored server-side, so each visitor pays for their own searches.

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

## Tests

The suite runs fully offline behind a socket-level network guard — any attempted network call fails the test immediately. Fixture-based replay providers stand in for live sources.

```bash
pip install pytest
pytest
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
assets/                           # Builder photos for the About page
providers/
  base.py                         # ScraperProvider / NihProvider / RolesProvider / HIndexProvider ABCs
  generic_faculty_scraper.py      # Agent 1: directory scrape + web search, unioned
  playwright_scraper.py           # Deterministic HTML directory parsing
  faculty_merge.py                # Name normalization, cross-source merge, dedupe
  url_discovery.py                # Finds a school's faculty directory URL
  live.py                         # Live network implementations (NIH, Scopus, OpenAlex, roles)
  replay.py                       # Fixture-based replays for offline tests
tools/
  discover_faculty_urls.py        # CLI: bulk directory-URL discovery
tests/                            # Offline suite (network-blocked): merge, scraper, email extraction
docs/                             # OVERVIEW · ARCHITECTURE · USAGE · PRD
Procfile, render.yaml             # Deployment (gunicorn on Render)
```

## License

[MIT](LICENSE) © 2026 Eric Markarian and Chehan Wijayaratne. Use it, fork it, deploy it at your institution — attribution appreciated.
