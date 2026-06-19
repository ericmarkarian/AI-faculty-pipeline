# Code Quality Report
_Last reviewed: 2026-04-07 — checked against commit `def7b91`_
_Not tracked in git. Update this file after each review pass so the next check is incremental._

---

## Open Issues

### HIGH

- [ ] **H1 — Duplicated institution lookup tables**
  `faculty_search.py:48-95` + `providers/live.py:19-65`
  `INSTITUTION_NIH_NAMES` and `INSTITUTION_KEYWORDS` are copy-pasted between both files and already diverging.
  **Fix:** extract to `data/institutions.py`, import in both callers.

- [ ] **H2 — Dead code: `get_nih_grants` and `get_scopus_hindex`**
  `faculty_search.py:148-299`
  ~150 lines never called — pipeline uses `LiveNihProvider` and `LiveHIndexProvider` exclusively.
  **Fix:** delete both functions.

- [ ] **H3 — Latent `NameError` on `_discipline`**
  `faculty_search.py:446`
  `_discipline` is only in scope in `run_pipeline` but referenced inside `agent2_enrich`. Crashes at runtime whenever `profile_scraper_roles is None`.
  **Fix:** add `discipline: str = ""` param to `agent2_enrich` and pass it through from `run_pipeline`.

- [ ] **H4 — In-place mutation of role dicts**
  `providers/live.py:190-193`
  `role.setdefault(...)` mutates parsed JSON response objects.
  **Fix:** `[{**r, "retrieved_at": r.get("retrieved_at", now), "model": r.get("model", _LIVE_MODEL)} for r in roles]`

- [ ] **H5 — Silent `None` API key**
  Multiple files — `os.environ.get("OPENAI_API_KEY")` returns `None` silently; crashes deep in a thread pool.
  **Fix:** use `os.environ["OPENAI_API_KEY"]` or raise `ValueError` at construction time.

---

### MEDIUM

- [ ] **M1 — `agent2_enrich` is 125 lines**
  `faculty_search.py:388-514` — mixing provider init, parallel fetching, role categorisation, metrics, result building.
  **Fix:** extract `_categorise_roles(roles)` and an observability helper.

- [ ] **M2 — Role categorisation duplicated inside `agent2_enrich`**
  `faculty_search.py:456-461` and `476-481` — same four filter lines, twice.
  **Fix:** call a shared `_categorise_roles()` helper from both paths.

- [ ] **M3 — `run_pipeline` is 115 lines**
  `faculty_search.py:648-762` — output writing, CSV/JSON, stats printing all inlined.
  **Fix:** extract `_write_outputs(final, df)` and `_print_coverage_stats(final, m)`.

- [ ] **M4 — Module-level mutable `_roles_config_cache`**
  `providers/live.py:100` — survives across test runs.
  **Fix:** replace with `@functools.lru_cache` on `_load_roles_config`.

- [ ] **M5 — `RolesProvider` interface mismatch**
  `providers/base.py:67` vs `providers/live.py:171` — `LiveRolesProvider.get_roles` adds `discipline` param not declared in abstract base.
  **Fix:** add `discipline: str = ""` to the base class signature.

- [ ] **M6 — `_parse_json` duplicated across 3 files**
  `faculty_search.py`, `providers/live.py`, `providers/generic_faculty_scraper.py`
  **Fix:** extract to `providers/utils.py`.

- [ ] **M7 — `_clean_name_parts` duplicated with diverging credential lists**
  `faculty_search.py:153` + `providers/live.py:122`
  **Fix:** single implementation in `providers/utils.py`.

- [ ] **M8 — `LiveHIndexProvider.get_hindex` is 127 lines**
  `providers/live.py:272-398`
  **Fix:** decompose into `_search_scopus_candidates`, `_score_and_select_candidate`, `_fetch_hindex_for_author`.

- [ ] **M9 — `-> any` should be `-> Any`**
  `faculty_search.py:129` — `any` is a built-in, not a type annotation.
  **Fix:** `from typing import Any` and use `-> Any`.

- [ ] **M10 — ~60 `print()` calls in production code**
  5+ files — `observe.log()` already exists.
  **Fix:** route through `logging` module or `observe.log`; eliminates interleaved thread output.

- [ ] **M11 — `isinstance(scraper, TavilyProfileScraper)` in orchestrator**
  `faculty_search.py:433` — concrete type check leaks into orchestrator.
  **Fix:** add `is_implemented() -> bool` to `ProfileScraper` base class.

---

### LOW

- [ ] **L1** — Large commented-out cache infrastructure in `faculty_search.py:137-141` — remove or move to a branch.
- [ ] **L2** — `# TODO: implement via Tavily` in `providers/profile_scrapers/fallback.py:26` has no ticket reference.
- [ ] **L3** — URL routing duplicated between `playwright_scraper.py` and `data/faculty_urls.json`; `PlaywrightScraperProvider` may be superseded.
- [ ] **L4** — `_scraper_provider` constructed at module import time (`faculty_search.py:306`) — makes unit testing harder; should be lazily initialised.
- [ ] **L5** — `observe.py` module-level mutable globals (`_metrics`, `RUN_ID`) — multiple pipeline runs in one process share/accumulate metrics.

---

## Cohesion Notes

| What | Where it lives now | Where it should live |
|------|--------------------|----------------------|
| Institution lookup tables | `faculty_search.py` + `providers/live.py` | `data/institutions.py` |
| Scraper dispatch / factory | `faculty_search.py:381-385` | `providers/profile_scrapers/__init__.py` |
| `generate_faculty_summary`, `_parse_roles` | `faculty_search.py:561-641` | `reporting.py` |
| `_parse_html` (imported as private across modules) | `providers/playwright_scraper.py` | Promote to public API |
| Scoring helpers (`_affiliation_score`, etc.) | `providers/live.py:197-246` | `providers/scoring.py` |

---

## Resolved Issues

_Move items here (with date and commit) once fixed, so future reviews skip them._

| Date | Commit | Issue |
|------|--------|-------|
| —    | —      | —     |
