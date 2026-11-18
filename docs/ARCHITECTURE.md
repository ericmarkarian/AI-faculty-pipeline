# ATLAS — Architecture & Internals

A technical walkthrough of how the pipeline discovers, enriches, and verifies faculty data.
This document reflects the **actual code**, which has moved ahead of the original
[PRD.md](PRD.md) and top-level README in several places (noted inline).

---

## Top-level flow

Everything is orchestrated from [`faculty_search.py`](../faculty_search.py). Two entry points:

- [`run_pipeline()`](../faculty_search.py) — full sweep: scrape schools → enrich → QC → export.
- [`run_pipeline_from_records()`](../faculty_search.py) — "names mode": enrich + QC a
  hand-supplied list of `{full_name, school}` records (skips Agent 1).

Both are exposed over a Flask Server-Sent-Events server ([`server.py`](../server.py)) that
streams each enriched record to the browser UI ([`medfaculty.html`](../medfaculty.html)) as
it completes.

```
run_pipeline(schools, discipline)
  ├─ Agent 1: agent1_scrape(school)          # sequential per school
  ├─ global dedup by (name, school)
  ├─ run_pipeline_from_records(...)
  │    ├─ ThreadPoolExecutor(MAX_WORKERS=8)  # parallel across faculty
  │    │    └─ agent2_enrich(faculty)        # per-person enrichment
  │    └─ agent3_qc(enriched)                # verify + cohort cross-checks
  └─ write CSV / JSON / summaries + alerts + metrics
```

---

## Agent 1 — Discovery

Implemented by [`GenericFacultyScraper`](../providers/generic_faculty_scraper.py). Two
independent sources run on **every** school and are unioned: a **directory scrape**
(render-then-extract) and a **GPT-4o web search**.

1. **URL resolution** — look up `(discipline, school)` in
   [`data/faculty_urls.json`](../data/faculty_urls.json). If absent, ask GPT-4o + web search
   for up to two directory URLs: a **university** page (preferred — richer academic bios) and
   a **hospital** specialist page (secondary). The result is cached back to the JSON file.
2. **Fetch** — headless Chromium (Playwright) loads the page with `wait_until="networkidle"`
   so JS-rendered directories fully populate. An error status or a bot-challenge page
   (Cloudflare "Just a moment…", "Access Denied", a 403) raises `DirectoryBlocked` instead
   of being parsed: a block page reads as "zero faculty" and would otherwise make the
   school look empty rather than unread.
3. **Compress** — `_html_to_text()` strips `script/style/nav/footer`, but **inlines hrefs**
   as `text [https://url]` so the LLM can still capture each person's individual profile
   link. This collapses a directory from ~100 raw-HTML chunks to 1–3 text chunks.
4. **Extract** — GPT-4o pulls `full_name`, `title`, `profile_url`, filtering to people whose
   **primary** discipline matches (excludes "by courtesy", visiting faculty, residents,
   fellows, staff). Pagination (`?page=N` / `/page/N`) is followed.
5. **Web search (always, in parallel)** — GPT-4o with `web_search_preview` searches for the
   same roster directly, reaching division pages, hospital microsites and news items that the
   configured URLs never touch. It runs on a background thread so its latency overlaps the
   browser fetches, and a failure in either source never discards the other.
   Set `AGENT1_WEB_SEARCH=0` (or `GenericFacultyScraper(use_web_search=False)`) to scrape only.
6. **Merge** — [`providers/faculty_merge.py`](../providers/faculty_merge.py) unions the
   sources. Records match on a normalized name that ignores degrees ("Aye, MD"), middle
   initials, accents, punctuation and `Last, First` ordering. Field precedence is
   `university > hospital > legacy url > web_search`: the most trusted source that has a
   value wins, lower-priority sources only fill blanks. Each record carries
   `discovery_source` (e.g. `university+web_search`) naming every source that found the person.

Agent 1 then dedups with the same name matching and excludes emeritus/emerita/courtesy titles.

**Discovery report.** Each school gets a `discovery_summary()` — faculty found, the count
per source, and an `issues` list ("No faculty found", "Blocked: …", "Could not load: …",
"No faculty listed on: …"). It is written to the run log, raises `ZERO_FACULTY` /
`DIRECTORY_UNREAD` alerts, and is streamed to the UI, which lists the affected institutions
above the results. A school with issues still gets whatever web search found, so treat its
roster as incomplete.

**Roster pages are not profiles.** When a directory has no per-person pages, every record's
`profile_url` is the roster itself (or an anchor on it). Those records are marked
`profile_url_shared` and Agent 2 skips the profile scrape for them — reading the roster as
a bio gave everyone on it the first person's title and roles.

> **Why both sources:** neither is a superset of the other. Web search used to be a fallback
> that fired only when the browser fetch failed, so a batch school search returned fewer
> people than looking the same faculty member up by hand — the batch/manual inconsistency.

> **Note vs. README:** the README describes Agent 1 as pure GPT-4o web search. The real path
> is Playwright + GPT-4o HTML extraction **and** GPT-4o web search, unioned.

---

## Agent 2 — Enrichment

[`agent2_enrich()`](../faculty_search.py) runs four lookups per person. Faculty are processed
across 8 worker threads; within each person, NIH + roles run in an inner 2-thread pool.

### 2.1 NIH grants — `LiveNihProvider`

Two-phase identity resolution against the free **NIH Reporter API**
([`providers/live.py`](../providers/live.py), `get_grants`):

**Phase 1 — name + institution (high confidence).**
Search PI name **and** institution org names. Org aliases come from
[`medical_schools_lookup.xlsx`](../medical_schools_lookup.xlsx): school name + primary
teaching hospital + affiliated hospitals (so "Stanford" also matches "Lucile Packard"). If
anyone returns, the person genuinely has grants there → take the best name match and pull
**all** their grants by `profile_id` (paginated). Marked institution **confirmed**.

**Phase 2 — name only, scored (broad fallback, flagged).**
If Phase 1 is empty (person moved, or holds no grants here), search by name only and score
each candidate profile:

```
combined = 0.50 · name_score  +  0.35 · inst_score  +  0.15 · dept_score
```

- `inst_score` — **1.0** confirmed at the target school, **0.3** confirmed at a *different*
  known school (→ wrong person, reject), **0.5** institution unknown.
- `dept_score` — **1.0** if NIH `dept_type` matches discipline words, else **0.5** (neutral
  when absent — hospital grants have no dept_type).
- **Thresholds:** `0.60` when institution is confirmed, **`0.78` strict** when unknown.
- **Ambiguity guard:** if the top two candidates are within `0.05` and institution is
  unconfirmed → skip rather than guess.

Grants returned via Phase 2 are tagged `institution_unconfirmed=True`, which becomes a yellow
QC signal downstream.

### 2.2 H-index — `LiveHIndexProvider`

**Scopus first, OpenAlex fallback.** Scopus offers curated author disambiguation; OpenAlex is
the free fallback used when Scopus is unavailable (no key, rate-limited, HTTP error) or
inconclusive (person not found at this institution under this name).

- **Name parsing** strips credentials and extracts `(first, middle_initial, last)`. Handles
  preferred names via parenthetical/quoted extraction — `Cecilio (Leo) Lopez → Leo`,
  `Azadeh "Azy" Issapouru → Azy` — and retries with the alternate first name if the primary
  fails the affiliation gate.
- **Scopus ranking** (`_scopus_lookup`): `name_score + affil_score`, with subject-area as a
  tiebreak. The winner **must** carry an affiliation signal (`affil_score > 0`) or the lookup
  returns `None` → OpenAlex. When a person is fragmented across tied profiles, pick the one
  with the **highest h-index** (the real profile holds the real publication record).
- **Affiliation matching** drops generic words ("university", "medical", "hospital") and
  requires **all** distinctive words of an alias to be present — so `johns hopkins` matches
  "Johns Hopkins University" but `johns hopkins bayview` does not.
- Confidence labeled `high / medium / needs_review`, embedded in `evidence_text`. The source
  (`Scopus` vs `OpenAlex`) is recorded on the record as `h_index_source`.

> **Note vs. README:** the README lists Scopus h-index as "disabled". It is enabled and is the
> primary source; OpenAlex is the fallback.

### 2.3 Roles — `LiveRolesProvider` + profile scrapers

Roles are bucketed into four categories: **editorial · society · leadership · training**.
Two paths, cheapest first:

1. **Profile scraper** ([`PlaywrightProfileScraper`](../providers/profile_scrapers/playwright_scraper.py))
   — if a profile URL exists, fetch the bio page and extract roles + email + education in one
   *local* GPT-4o call (no web search — cheaper, deterministic).
2. **LLM web search** (`_ROLES_PROMPT`) — fallback. The prompt is engineered against the
   system's biggest failure mode, **section-heading misattribution**: it explicitly warns that
   "Director" / "Chief" / "Faculty" on a team-listing page are usually *headings grouping many
   people*, not roles held by each person, and a role is only valid if the source text ties it
   to the named person. It requires a **verbatim ≤800-char evidence excerpt** per role (no
   paraphrase) so QC can independently verify. Excludes academic ranks, general affiliation,
   credentials, and "former/past/emeritus" roles or closed past date ranges.

**Specialty guidance.** Each discipline has a config in [`data/roles/`](../data/roles/)
(e.g. [`pediatric_endocrinology.md`](../data/roles/pediatric_endocrinology.md)) listing target
journals / societies / leadership roles, injected into the roles prompt. A physician can add
free-text "considerations" at runtime; `merge_specialty_considerations()` uses GPT-4o-mini to
permanently fold genuinely-new entries into the `.md` file.

**Email** is pulled from raw HTML (`mailto:` regex), with a Playwright button-click fallback.
**Education** (med school / residency / fellowship) comes from the same profile extraction.

### 2.4 Enriched record shape

```jsonc
{
  "full_name": "...", "school": "...", "title": "...", "email": "...",
  "education": [ { "type": "Fellowship", "institution": "...", "specialty": "...", "year": 2009 } ],
  "h_index": 42, "h_index_source": "Scopus",
  "nih_project_nums": "R01...,R01...", "nih_grant_titles": "... | ...",
  "nih_grant_count": 3, "nih_institution_unconfirmed": false,
  "editorial_roles": [...], "society_roles": [...],
  "training_roles": [...], "leadership_roles": [...]
}
```

Each role object carries `category, role, org, source_url, evidence_text, retrieved_at`.

---

## Agent 3 — QC

[`providers/qc.py`](../providers/qc.py) — a RAGAS-style **faithfulness verifier**. It does not
trust Agent 2; it independently re-checks every claim. `verify_faculty_roles()` produces a
`green / yellow / red` signal per category and an `overall`.

### Per-role checks

For every role:

1. **URL liveness** — does `source_url` return 2xx? (403 = bot-blocked = inconclusive, not a
   failure.)
2. **Name on page** — does the person's name (or a variant) appear in `evidence_text` or the
   URL? Catches the LLM citing the wrong page.
3. **Role on page** — do ≥50% of the role's distinctive tokens appear in the evidence?
4. **Negation guard** — "former / previously / stepped down / retired", or a closed date range
   ending before 2020 near the role → caps the role at yellow.

Status logic (`_status_from_checks`):
- **green** = URL alive + name on page + role on page + no negation
- **red** = no evidence, OR URL resolves but the name is absent (wrong page)
- **yellow** = everything in between

### Cross-signal checks

- **NIH confidence** — grants at a confirmed institution = green; `institution_unconfirmed` =
  yellow ("may be from a previous institution; human review recommended").
- **H-index confidence** — found = green; not found = yellow.
- **NIH ↔ h-index consistency** — a PI with **≥6 grants** but a near-zero h-index is
  contradictory (likely a mismatched record), surfaced as yellow on the h-index signal
  (the value most likely wrong). **One-directional:** high h-index + few grants is legitimate
  (foundation/industry/international funding) and is never flagged.

### Cohort-level checks (`_qc_cross_checks`)

Run after per-person verification, across the whole result set:

- **Duplicate singleton roles** — if two people both claim a singleton role
  (Chief/Chair/President/Director/Dean/Program Director) at the same org, both are flagged.
- **Completeness / sparse profile** — a senior person (`h_index ≥ 30` or `nih_grant_count ≥ 5`)
  with **zero** roles is likely a scraping failure, not genuine inactivity → a vacuously-green
  record is downgraded to yellow.

### QC output

The nested `qc` dict is flattened into three CSV columns, each `"<confidence> — <why>"`:

- `qc_nih`, `qc_hindex`, `qc_roles`

`qc_roles` lists every role with its link and a pass/fail reason. The "why" is populated even
for passing cases, so a reviewer always sees the basis for a green.

> **Note vs. README:** Agent 3 is described in the README as a disabled GPT-4o mega-prompt.
> The real Agent 3 is an enabled, mostly-deterministic verifier (the only LLM-adjacent piece
> is the evidence Agent 2 captured); it has no token-limit problem.

---

## Observability & alerting

[`observe.py`](../observe.py). Each run gets a unique `RUN_ID` and a structured JSONL log in
[`logs/`](../logs/) (one file per run). Thread-safe metrics track cost (estimated, ~$0.025 per
web-search call), latencies, API errors, cache hit/miss, and field coverage.

`run_alert_checks()` fires on:

| Alert | Threshold |
|-------|-----------|
| `LOW_DISCOVERY` | < 10 faculty found |
| `HIGH_ERROR_RATE` | > 10% of API calls failed |
| `COST_EXCEEDED` | > $5.00 LLM cost |
| `LOW_NIH_COVERAGE` | < 30% of faculty have grants |

These catch directory-structure breakage and runaway cost.

---

## Outputs

- [`faculty_output.csv`](../faculty_output.csv) — tabular, with flattened QC columns.
- [`faculty_output.json`](../faculty_output.json) — full structured data, including nested `qc`.
- [`faculty_summaries.txt`](../faculty_summaries.txt) — plain-language paragraphs built
  **without any new LLM call** (`generate_faculty_summary` just templates collected data).
- **Live UI** — `server.py` streams each record over SSE as it's enriched and again after QC.

---

## Provider abstraction

All network sources sit behind abstract bases in [`providers/base.py`](../providers/base.py)
(`ScraperProvider`, `NihProvider`, `RolesProvider`, `HIndexProvider`). `providers/live.py`
holds the network implementations; `providers/replay.py` holds fixture-based replays used by
the offline test suite (see [USAGE.md](USAGE.md)). This is what lets the test suite run
fully offline with a network guard.

---

## Known current-state caveats

Honest notes for anyone reading the code (tracked in
[`CODE_QUALITY.md`](../CODE_QUALITY.md)):

- **Caching is disabled.** The SQLite cache with per-field TTL (NIH 30d / h-index 90d /
  roles 7d) is built but commented out — every run is a cold run, and the cache hit-rate
  metrics are always ~0.
- **Agent 1 is sequential** across schools (a known scaling bottleneck).
- **No resume/checkpoint** — a crash mid-run restarts from zero.
- **`get_scopus_hindex` in `faculty_search.py` is dead code** — the live path is
  `LiveHIndexProvider`.
- **Institution lookup tables are duplicated** between `faculty_search.py` and
  `providers/live.py`, despite a consolidated `providers/institutions.py` existing.

These are the natural starting points for the roadmap.
