# ATLAS — Usage Guide

How to set up and run the pipeline, both from the browser UI and the command line.

---

## 1. Prerequisites

```bash
pip install -r requirements.txt
playwright install chromium     # one-time browser download for the scraper
```

The pipeline calls several external services. Set these as environment variables (a `.env`
file in the repo root is loaded automatically):

| Variable | Required? | Used for |
|----------|-----------|----------|
| `OPENAI_API_KEY` | **Yes** | GPT-4o discovery, roles extraction, profile parsing |
| `SCOPUS_API_KEY` | Recommended | Primary h-index source. Without it, h-index falls back to OpenAlex |
| `SCOPUS_INSTTOKEN` | Optional | Scopus institutional token (off-network access) |
| `OPENALEX_API_KEY` | Optional | Higher-concurrency OpenAlex pool; works without it (polite pool) |
| `SITE_PASSWORD` | Optional | If set, the web UI requires this password (HTTP basic auth) |
| `PORT` | Optional | Web server port (default `5050`) |
| `AGENT1_WEB_SEARCH` | Optional | `0` runs Agent 1 discovery from the scraped directory pages only. Default `1` — directory scrape **and** GPT-4o web search, unioned (one extra web-search call per school) |

> **Scopus note:** the Scopus API generally requires your institution's network or VPN
> (or a valid `SCOPUS_INSTTOKEN`). If Scopus is unavailable, the pipeline automatically
> falls back to OpenAlex for h-index — it does not fail.

Example `.env`:

```bash
OPENAI_API_KEY=sk-...
SCOPUS_API_KEY=...
# SCOPUS_INSTTOKEN=...
# OPENALEX_API_KEY=...
# SITE_PASSWORD=...
```

Never commit real keys — `.env` is gitignored.

---

## 2. Run from the browser (recommended)

```bash
python3 server.py
# → [Server] Starting at http://localhost:5050
```

Open **http://localhost:5050**. The UI supports two modes:

### Mode A — Discipline sweep
1. Pick one or more **institutions**.
2. Pick a **discipline**.
3. (Optional) Add free-text **considerations** — e.g. "prioritize ISPAD committee roles".
   In a discipline sweep these are merged permanently into that specialty's guidance file.
4. Run. Results **stream in live** — each card appears as the person is enriched, then
   updates again with its green/yellow/red QC badge.

### Mode B — Names mode
Paste a list of **name + institution** pairs to enrich a specific set of people (skips
discovery). Considerations here are applied as one-time guidance for that run only.

---

## 3. Run from the command line

The default target is set at the top of [`faculty_search.py`](../faculty_search.py):

```python
SCHOOLS    = ["Stanford University School of Medicine"]
DISCIPLINE = "Pediatric Endocrinology"
MAX_WORKERS = 8     # concurrent faculty enrichments
```

Then:

```bash
python3 faculty_search.py
```

To change targets, either edit those constants, or call the API directly:

```python
from faculty_search import run_pipeline

run_pipeline(
    schools=["Yale School of Medicine", "Duke University School of Medicine"],
    discipline="Cardiology",
    considerations="",   # optional specialty guidance
)
```

To enrich a hand-supplied list (names mode) without scraping a directory:

```python
from faculty_search import run_pipeline_from_records

records = [
    {"full_name": "Jane Smith", "school": "Stanford University School of Medicine"},
    {"full_name": "John Doe",   "school": "Yale School of Medicine"},
]
run_pipeline_from_records(records, discipline="Cardiology")
```

---

## 4. What you get

After a run, three files are written to the repo root:

| File | Contents |
|------|----------|
| [`faculty_output.csv`](../faculty_output.csv) | Tabular, with flattened `qc_nih` / `qc_hindex` / `qc_roles` columns |
| [`faculty_output.json`](../faculty_output.json) | Full structured data, including the nested `qc` object per person |
| [`faculty_summaries.txt`](../faculty_summaries.txt) | One plain-language paragraph per faculty member |

Plus a per-run structured log at `logs/pipeline_<RUN_ID>.jsonl`.

### Reading the QC columns

Each QC column is formatted `"<confidence> — <why>"`:

- **green** — verified (URL live, name + role found on the cited page, no negation)
- **yellow** — needs human review (e.g. h-index not found, NIH institution unconfirmed,
  grants↔h-index mismatch, or a sparse senior profile)
- **red** — unverified (no evidence, or the cited page doesn't mention the person)

`qc_roles` lists every role with its source link and the reason it passed or failed, so you
can spot-check any claim in seconds.

---

## 5. Adding institutions or specialties

**New institution.** Add it to the lookup tables so matching works:
- `INSTITUTION_KEYWORDS` in [`faculty_search.py`](../faculty_search.py) and
  `_INSTITUTION_KEYWORDS` / `_INSTITUTION_NIH_NAMES` in
  [`providers/live.py`](../providers/live.py) (Scopus keyword + NIH org aliases).
- Ideally also add a row to [`medical_schools_lookup.xlsx`](../medical_schools_lookup.xlsx)
  (school + teaching/affiliated hospitals) — this is the primary alias source for NIH and
  h-index affiliation matching. Institutions not in the xlsx fall back to an OpenAlex lookup.
- If the UI displays a different school name than the JSON key, add a mapping in
  `_SCHOOL_NAME_MAP` in [`server.py`](../server.py).

**New specialty.** Create `data/roles/<specialty_slug>.md` (lowercase, spaces→underscores)
following the structure of an existing file like
[`pediatric_endocrinology.md`](../data/roles/pediatric_endocrinology.md): target societies,
target journals, leadership roles. It's injected into the roles prompt automatically. You can
also seed it at runtime by passing `considerations` on the first run for that discipline.

**Faculty directory URLs** are auto-discovered and cached to
[`data/faculty_urls.json`](../data/faculty_urls.json) on first run. To force a specific URL,
edit that file directly.

---

## 6. Offline tests (golden set)

A zero-network test harness validates roles extraction and NIH enrichment against a fixed
5-person golden set. Fixtures are captured once (requires network), then tests run fully
offline behind a network guard.

```bash
pip install pytest pyyaml

# One-time fixture capture (requires network):
python -m tools.capture_fixture_roles --name "David Maahs" --school "Stanford" \
    --out tests/fixtures/roles/david_maahs.json
python -m tools.capture_fixture_nih   --name "Darrell Wilson" --school "Stanford" \
    --out tests/fixtures/nih/darrell_wilson.json
# ...repeat for the other golden-set members (see top-level README)

# Run offline:
pytest tests/test_golden_set.py -v
```

Tests fail immediately if any network call is attempted. Snapshot regressions print a
unified diff so prompt/model changes are visible; delete a snapshot under `tests/snapshots/`
to re-baseline after an intentional change.

---

## 7. Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `OPENAI_API_KEY not set` | Export it or add to `.env` |
| All h-index values from OpenAlex | Scopus key missing or off-network — set `SCOPUS_API_KEY` / `SCOPUS_INSTTOKEN`, or accept the OpenAlex fallback |
| `LOW_DISCOVERY` alert / very few faculty | Directory page structure changed or the wrong URL was cached — check/clear the entry in `data/faculty_urls.json` |
| Many `institution_unconfirmed` NIH flags | Institution aliases missing from the xlsx / lookup tables — add them |
| Playwright errors on first run | Run `playwright install chromium` |
| Web UI asks for a password | `SITE_PASSWORD` is set in the environment |

See [ARCHITECTURE.md](ARCHITECTURE.md) for how each stage works internally, and
[OVERVIEW.md](OVERVIEW.md) for the high-level summary.
