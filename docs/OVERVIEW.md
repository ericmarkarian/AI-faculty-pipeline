# ATLAS — Overview

**ATLAS** (**A**cademic **T**alent **L**ocation **A**nd **S**ourcing) is an automated
faculty-discovery and enrichment pipeline for academic medicine, built at Stanford Clinical
Informatics. Given a **discipline** (e.g. "Pediatric
Endocrinology") and a set of **institutions**, it returns a structured, evidence-backed
dataset of all relevant faculty — in minutes instead of hours.

---

## The problem

Faculty recruiting in academic medicine runs on informal, word-of-mouth networks. That
systematically overlooks qualified candidates outside an institution's existing network,
regardless of their merit. Recruiters spend hours hand-searching faculty directories,
cross-referencing NIH databases, and verifying credentials one person at a time.

## What it does

ATLAS replaces "who do we know" with a systematic sweep. It discovers every faculty
member at a target institution + discipline, then enriches each profile with **objective,
sourced metrics**:

- **NIH grant history** (NIH Reporter)
- **H-index** (Scopus, with OpenAlex fallback)
- **Named professional roles** — editorial boards, society offices, institutional
  leadership, training-program directorships
- **Email** and **education** (medical school / residency / fellowship)

Every claim is independently re-verified and tagged with a **green / yellow / red**
confidence signal before it reaches the user.

## Design principle: discovery, not ranking

ATLAS is intentionally **not** an evaluation or ranking system.

- **Objective metrics only** — verifiable data from named external databases. No composite
  "quality score."
- **Filter, don't rank** — users explore by criteria (active NIH funding, leadership roles)
  but the system never produces an ordered list implying relative merit.
- **Transparency** — every data point is traceable to its source URL.
- **Bias reduction by design** — by surfacing *all* matching faculty regardless of network
  connections, the tool is meant to reduce — not replicate — network bias in recruiting.

## How it works, in one breath

A three-agent pipeline: **Agent 1** discovers faculty from official directory pages,
**Agent 2** enriches each person from NIH / Scopus / OpenAlex / the open web, and **Agent 3**
independently verifies every claim and assigns confidence. Results stream live to a browser
UI and export to CSV / JSON.

```
Discipline + Institutions
        │
        ▼
   Agent 1 — Discovery      (render directory → extract faculty)
        │
        ▼
   Agent 2 — Enrichment     (NIH grants · h-index · roles · email · education)
        │
        ▼
   Agent 3 — QC             (verify every claim → green / yellow / red)
        │
        ▼
   CSV · JSON · live UI
```

## Current coverage

- **50 medical specialties** configured with target journals / societies / leadership roles
- **~30 top institutions** mapped (school + affiliated children's hospitals)
- Processes **~26 faculty in ~16 seconds**

## What makes it trustworthy

The whole design is organized around defending three failure modes inherent to
LLM-driven data collection:

| Risk | Defense |
|------|---------|
| Hallucinated faculty / roles | Verbatim evidence required + independent re-verification |
| Wrong-person attribution (name collisions) | Scored matching with institution gating |
| Silent scraping failures | Completeness checks + automated coverage/cost/error alerts |

---

See [ARCHITECTURE.md](ARCHITECTURE.md) for how it works internally, and
[USAGE.md](USAGE.md) for how to run it.
