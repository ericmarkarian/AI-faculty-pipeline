# Product Requirements Document
## AI Faculty Discovery & Enrichment Pipeline

**Version:** 1.0
**Status:** Active Development
**Last Updated:** March 2026

---

## Executive Summary

The AI Faculty Pipeline is an automated system for discovering and enriching academic faculty profiles across universities and medical departments. It replaces manual, word-of-mouth faculty discovery with a systematic, data-driven approach — surfacing candidates based on objective metrics (NIH funding, h-index, society leadership, editorial roles) rather than existing networks.

The current prototype successfully processes ~26 faculty in ~16 seconds using parallel enrichment and an SQLite caching layer. The goal is to scale to thousands of institutions and tens of thousands of faculty without proportional increases in runtime or cost.

---

## Problem Statement

Faculty recruitment in academic medicine relies heavily on informal networks. This creates systemic bias — candidates outside an institution's existing network are systematically overlooked, regardless of their qualifications. Recruiters spend hours manually searching faculty directories, cross-referencing NIH databases, and verifying credentials.

There is no existing tool that:
- Automatically discovers all faculty at a given institution + discipline
- Enriches profiles with objective research and leadership metrics
- Scales across hundreds of institutions without manual intervention
- Maintains fresh, deduplicated data with intelligent caching

---

## Target Users

| User | Need |
|---|---|
| Faculty Recruiters | Identify qualified candidates across institutions quickly |
| Department Chairs | Benchmark their faculty against peer institutions |
| Research Administrators | Track NIH funding landscapes across disciplines |
| Academic Search Committees | Build diverse, comprehensive candidate pools |

---

## Core Product Vision

A fully automated pipeline that, given a discipline (e.g. "Pediatric Endocrinology") and a list of target institutions, produces a structured, enriched, deduplicated dataset of all relevant faculty — searchable and filterable by research metrics, funding history, and leadership roles — in minutes rather than hours.

---

## Ethical Design Principles

The AI Faculty Pipeline is designed as a discovery and exploration tool, not an evaluation or ranking system.

- **Objective metrics only** — The system surfaces verifiable academic data (NIH grant history, publication counts, editorial and society roles) without generating composite scores or quality rankings.
- **Discovery over evaluation** — The goal is to broaden the candidate pool by making more faculty visible, not to score or rank individuals against each other.
- **Filter, don't rank** — Users can filter and explore by specific criteria (e.g., active NIH funding, leadership roles held) but the system does not produce ordered lists implying relative merit.
- **Transparency** — All data fields are sourced from named external databases (NIH Reporter, Scopus, public web). Users can inspect the source of any data point.
- **Bias reduction by design** — By systematically surfacing all faculty matching a discipline regardless of existing network connections, the pipeline is intended to reduce — not replicate — informal network bias in recruiting.

---

## Key Use Cases

1. **Recruiting sweep** — Given a discipline, find all faculty across 20 peer institutions and identify those matching specified research metrics such as NIH funding history or publication impact
2. **Landscape mapping** — Who holds leadership roles in professional societies for a given specialty?
3. **Benchmark report** — How does Stanford's Pediatric Endocrinology division compare to UCSF on research output?
4. **Incremental refresh** — Re-run pipeline weekly; only re-fetch stale data (cache TTL enforcement)

---

## Functional Requirements

### Agent 1 — Discovery (Scraper)
- [x] Accept a school name and discipline as input
- [x] Use GPT-4o web search to find all faculty at the target institution
- [x] Extract: full_name, title, subspecialty, email, profile_url
- [x] Deduplicate by (full_name, school)
- [ ] Support batch input of multiple schools in parallel
- [ ] Validate that faculty directory was actually reached (not a hallucinated list)
- [ ] Fall back to direct HTML scraping if web search returns incomplete results

### Agent 2 — Enrichment
- [x] NIH Reporter API — grant history by PI name + institution
- [x] Scopus API — h-index lookup (implemented, currently disabled)
- [x] Web search — editorial roles, society roles, training roles, leadership roles
- [x] SQLite cache with per-field TTL (NIH: 30d, h-index: 90d, roles: 7d)
- [x] Parallel execution across faculty (8 workers)
- [x] Concurrent NIH + roles search per faculty (asyncio.gather)
- [ ] PubMed — publication count, recent papers, citation count
- [ ] Semantic Scholar — citation metrics, research topic classification
- [ ] Resume mode — skip already-enriched faculty if run is interrupted

### Agent 3 — QC
- [x] Duplicate detection (implemented, currently disabled)
- [x] Conflict flagging (implemented, currently disabled)
- [x] Confidence scoring per faculty entry (implemented, currently disabled)
- [ ] Re-enable and batch QC to avoid single-call token limits
- [ ] Cross-institution deduplication (same person at two schools)

### Output
- [x] CSV export (faculty_output.csv)
- [x] JSON export (faculty_output.json)
- [ ] Incremental append mode (don't overwrite previous runs)
- [ ] Per-run audit log (timestamp, faculty count, cache hit rate, API errors)
- [ ] REST API endpoint for querying results
- [ ] Dashboard UI for browsing and filtering results

---

## Non-Functional Requirements

| Requirement | Target |
|---|---|
| Throughput | ≤ 2 min for 200 faculty (5 schools) |
| Cache hit latency | < 1s per faculty on warm cache |
| Uptime (if API exposed) | 99.5% |
| Data freshness | Roles: 7d, NIH: 30d, H-index: 90d |
| Deduplication accuracy | > 98% |
| Cost per 1,000 faculty | < $5 USD (LLM calls only) |

---

## System Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                        run_pipeline()                        │
│                                                              │
│  ┌──────────────┐    ┌──────────────────────────────────┐   │
│  │   Agent 1    │    │           Agent 2                 │   │
│  │   Scraper    │───▶│   ThreadPoolExecutor (8 workers)  │   │
│  │  (per school)│    │   ┌──────────────────────────┐   │   │
│  └──────────────┘    │   │  per-faculty enrichment   │   │   │
│                      │   │  asyncio.gather:           │   │   │
│                      │   │   ├─ NIH Reporter API      │   │   │
│                      │   │   └─ GPT-4o web search     │   │   │
│                      │   └──────────────────────────┘   │   │
│                      └──────────────────────────────────┘   │
│                                    │                         │
│                          ┌─────────▼──────────┐             │
│                          │   SQLite Cache      │             │
│                          │   faculty_cache.db  │             │
│                          └─────────────────────┘            │
│                                    │                         │
│                          ┌─────────▼──────────┐             │
│                          │   Agent 3 (QC)      │             │
│                          │   (disabled)        │             │
│                          └─────────────────────┘            │
│                                    │                         │
│                    faculty_output.csv / .json                │
└─────────────────────────────────────────────────────────────┘
```

**External APIs:**
- OpenAI Responses API (gpt-4o + web_search_preview)
- OpenAI Chat API (gpt-4o, Agent 3 QC)
- NIH Reporter API (free, no auth)
- Scopus Author Search + Profile API (institutional key required)

---

## Data Pipeline Architecture

```
SCHOOLS + DISCIPLINE
        │
        ▼
  Agent 1: Scrape
  (one LLM call per school)
        │
        ▼
  Deduplicate globally
        │
        ▼
  Agent 2: Enrich (parallel, 8 workers)
  For each faculty:
    1. Check SQLite cache
    2. If stale: asyncio.gather(NIH, roles_search)
    3. Write fresh data back to cache
        │
        ▼
  Agent 3: QC (disabled)
  Batch LLM pass for duplicate/conflict detection
        │
        ▼
  Output: CSV + JSON
```

---

## Scaling Strategy

### Current State (v1)
- 1 school, 1 discipline, ~26 faculty
- 8 parallel workers
- ~16s cold, ~0s warm
- Single-process, single-machine

### Target State (v2–v3)

**Parallelism**
- Parallelize Agent 1 (school scraping) — currently sequential
- Increase MAX_WORKERS dynamically based on observed rate limit headroom
- Add per-API rate limiters (token bucket) instead of global sleeps

**Caching**
- Migrate SQLite → PostgreSQL for multi-process / multi-machine support
- Add cache warming script (pre-populate before a big run)
- Cache Agent 1 faculty lists (TTL: 7 days) to avoid re-scraping directories

**Batching**
- Agent 3 QC: batch faculty in groups of 15 instead of all-at-once
- NIH API: explore batch name lookups if available

**Infrastructure**
- Containerize with Docker
- Run pipeline as a scheduled job (cron / Celery)
- Distribute across workers using a task queue (Redis + Celery or AWS SQS)

### Scaling Estimates

| Faculty Count | Schools | Cold (current) | Cold (v2, parallel schools) | Warm |
|---|---|---|---|---|
| 26 | 1 | 16s | 16s | 0s |
| 200 | 5 | ~2 min | ~30s | ~2s |
| 1,000 | 25 | ~10 min | ~90s | ~5s |
| 10,000 | 100+ | ~100 min | ~15 min | ~30s |

---

## API Strategy

If results are exposed externally (v3):

**Proposed Endpoints**

| Method | Path | Description |
|---|---|---|
| GET | /faculty | List all faculty (paginated, filterable) |
| GET | /faculty/{id} | Single faculty profile |
| GET | /faculty/search?q= | Full-text search |
| POST | /pipeline/run | Trigger a pipeline run |
| GET | /pipeline/status/{run_id} | Check run progress |

**Filtering parameters:** `school`, `discipline`, `min_nih_grants`, `min_h_index`, `has_leadership`, `has_editorial`

**Authentication:** API key per consumer (header: `X-API-Key`)

**Rate limiting:** 100 req/min per key

---

## Data Model

### Faculty Record

| Field | Type | Source | Notes |
|---|---|---|---|
| full_name | string | Agent 1 | Primary identifier |
| school | string | Agent 1 | Institution name |
| title | string | Agent 1 | Academic rank |
| subspecialty | string | Agent 1 | Clinical focus |
| email | string | Agent 1 | Often null |
| profile_url | string | Agent 1 | Faculty page URL |
| h_index | integer | Scopus | Disabled; TTL 90d |
| nih_project_nums | string | NIH Reporter | CSV of grant numbers |
| nih_grant_titles | string | NIH Reporter | Pipe-delimited titles |
| nih_grant_count | integer | NIH Reporter | Derived; TTL 30d |
| editorial_roles | array | GPT web search | TTL 7d |
| society_roles | array | GPT web search | TTL 7d |
| training_roles | array | GPT web search | TTL 7d |
| leadership_roles | array | GPT web search | TTL 7d |
| confidence_score | integer | Agent 3 QC | 0–100; disabled |
| duplicate_flag | boolean | Agent 3 QC | Disabled |
| conflict_flag | boolean | Agent 3 QC | Disabled |
| qc_notes | string | Agent 3 QC | Disabled |

### Cache Schema (SQLite `faculty_cache`)

| Column | Type | TTL |
|---|---|---|
| faculty_name | TEXT (PK) | — |
| institution | TEXT (PK) | — |
| nih_grants_json | TEXT | 30 days |
| nih_cached_at | TEXT | — |
| h_index | INTEGER | 90 days |
| h_index_cached_at | TEXT | — |
| editorial_roles | TEXT (JSON) | 7 days |
| society_roles | TEXT (JSON) | 7 days |
| training_roles | TEXT (JSON) | 7 days |
| leadership_roles | TEXT (JSON) | 7 days |
| roles_cached_at | TEXT | — |

---

## Cost and Performance Considerations

### LLM Costs (current, per full run)

| Call | Model | Qty | Est. Cost |
|---|---|---|---|
| Agent 1 scrape | gpt-4o + web search | 1 per school | ~$0.05 |
| Agent 2 roles search | gpt-4o + web search | 1 per faculty | ~$0.02–0.05 |
| Agent 3 QC | gpt-4o chat | 1 per run | ~$0.10 |

**Estimated cost for 26 faculty:** ~$0.75–1.50 per cold run, ~$0.05 per warm run (cache hit)

**At scale (1,000 faculty):** ~$25–50 cold, ~$1–2 warm

### Cost Reduction Strategies
- Cache aggressively — roles data changes slowly
- Use `gpt-4o-mini` for roles parsing where full reasoning isn't needed
- Batch multiple faculty into a single roles search call (trade accuracy for cost)
- Use Semantic Scholar (free) as primary publication source before Scopus

---

## Risk Analysis

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| OpenAI web search returns hallucinated faculty | Medium | High | Agent 3 QC + cross-reference with official directory |
| NIH API returns grants for wrong PI (name collision) | Medium | Medium | Add institution filter; manual review flag |
| Scopus rate limits hit at scale | High | Medium | Cache h-index aggressively (90d TTL); batch requests |
| Faculty directory page structure changes | High | High | Monitor for sudden drop in faculty count; alert on <50% expected |
| LLM cost overrun at scale | Medium | Medium | Per-run cost tracking; circuit breaker if cost > threshold |
| SQLite contention at high parallelism | Low | Low | WAL mode + write lock already implemented; migrate to Postgres at scale |

---

## Security and Compliance Considerations

- **API keys** — stored as environment variables only, never committed to repo (.gitignore enforced)
- **PII** — faculty email addresses are scraped from public directories; no private data collected
- **Web scraping** — pipeline uses LLM web search (not direct scraping); respectful of robots.txt by proxy
- **Scopus TOS** — institutional key required; results should not be redistributed without Elsevier agreement
- **NIH data** — public domain, no restrictions
- **Data retention** — SQLite cache stored locally; no cloud persistence in current version

---

## Metrics for Success

| Metric | v1 Target | v2 Target |
|---|---|---|
| Faculty discovered per school | > 90% of directory | > 95% |
| NIH grant coverage | > 50% of active researchers | > 70% |
| Roles coverage (any role found) | > 50% of faculty | > 65% |
| Cold run time (200 faculty) | < 3 min | < 45s |
| Warm run time (200 faculty) | < 10s | < 5s |
| Duplicate rate in output | < 5% | < 1% |
| Cost per 1,000 faculty (cold) | < $50 | < $20 |

---

## Architectural Bottlenecks (Current)

1. **Agent 1 is sequential** — schools are scraped one at a time; should parallelize
2. **No resume/checkpoint** — a crash at faculty #45 restarts from zero
3. **Agent 3 QC is a single mega-prompt** — will hit token limits at >50 faculty
4. **SQLite won't scale to multi-process** — must migrate to Postgres for distributed runs
5. **No observability** — no structured logging, no run history, no cost tracking
6. **Faculty list not cached** — Agent 1 re-scrapes directories on every run

---

## Opportunities for Automation

- **Scheduled runs** — weekly refresh of roles (7d TTL), monthly for NIH (30d TTL), quarterly for h-index
- **Alerting** — notify when a tracked faculty member gains a new NIH grant or leadership role
- **Auto-expand** — after processing a school, automatically discover peer institutions
- **Autonomous QC loop** — Agent 3 flags a record → Agent 2 re-fetches to resolve → Agent 3 re-scores

---

## Roadmap

### v1 — Current (Prototype)
- [x] Single school, single discipline
- [x] NIH grants, roles, h-index (Scopus disabled)
- [x] SQLite cache with TTL
- [x] Parallel enrichment (8 workers + asyncio.gather)
- [x] CSV + JSON output

### v2 — Scale (Next)
- [ ] Multi-school parallel scraping
- [ ] Re-enable and fix Scopus h-index
- [ ] PubMed publication enrichment
- [ ] Resume mode (checkpoint on crash)
- [ ] Re-enable Agent 3 QC with batching
- [ ] Structured run logging (cost, cache hit rate, errors)
- [ ] Docker containerization

### v3 — Platform
- [ ] REST API exposing faculty data
- [ ] Web dashboard (filter, sort, export)
- [ ] PostgreSQL backend (replace SQLite)
- [ ] Scheduled job runner (Celery + Redis)
- [ ] Multi-discipline support (parameterized runs)
- [ ] Alerting on faculty profile changes
- [ ] Semantic Scholar integration
