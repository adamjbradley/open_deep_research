# Design: searchable research substrate (Research Memory ①)

**Status:** designed (brainstormed). **Sub-project ① of 4** in the "read before you write"
program — making the fact base a first-class memory every run consults before it goes back to the
web. The four sub-projects and their dependency order:

```
① searchable substrate (THIS SPEC)  →  ② cross-run source cache (don't re-fetch)
                                     →  ③ KB-first research gate (research only the delta)
                                                                  →  ④ cross-subject source reuse
```

② / ③ depend on ① (nothing can be reused until it is findable); ④ depends on ① and benefits from
②. This spec covers **only ①**.

## Problem

The pipeline is effectively **write-only**: it persists almost everything and reads back almost
nothing. Verified against `main`:

| Output | Stored at | Reused on a later run? |
|---|---|---|
| Raw source text (full fetched page) | `run_source.text` (`factbase/schema.py`, written by `RunSourceStore.record`, `factbase/store.py:10-21`) | **No** — keyed per-run; never consulted again |
| Compressed research notes | `research_runs.raw_notes` (`storage.py`) | **No** — archived, never queried |
| Rolled-up dossier (long-form) | `subjects.current_report` (`storage.py`) | Only in prose mode (the `assess_knowledge` cache-hit, `nodes/brief.py:111-182`) |
| Extracted facts (+ per-fact `narrative`) | `fact` table (`factbase/schema.py:65-81`) | Lookup by exact `instance_key + property_name` only (`factbase/query.py`); semantic conflict-dedup at ingest |

The duplication the team feels comes from three downstream gaps (no KB-first retrieval; no
cross-run fetch-skip; **no search index** so stored content is not retrievable by keyword or
meaning). **This spec closes the foundational one: stored research is not searchable.** Today you
can only fetch a fact by its exact key — you cannot ask "what do we already know that mentions
ROCA / eIDAS / population basis?" Without that, ②/③/④ have nothing to query.

## Goal

A **read-only FTS index over content the pipeline already persists**, plus a single **query API**
(and a thin CLI over it), so prior research becomes findable by keyword. Purely additive: the
research loop does not change. On its own this delivers facet **B** (the long-form raw research
becomes retrievable instead of write-once) and provides the substrate ③ will query.

## Decisions (from brainstorming)

- **FTS5 (keyword), behind a vector-ready interface.** SQLite FTS5 is built into the DB already in
  use — no new infrastructure, no embedding-model dependency (the project already wrestles with
  model failover), deterministic, cheap, and strong on the proper-noun-dense content here (country
  names, statute titles, regulators, "ROCA", source domains). The query API hides the engine so a
  semantic/vector adapter can slot in **behind the same interface** later (a future sub-project),
  without touching callers. Weakness accepted for v1: pure paraphrase/synonym recall.
- **The real artifact is a query API; a thin `dossier search` CLI proves it.** The library function
  is what ②/③ will consume; the CLI makes ① demoable and end-to-end testable now without touching
  the research loop. The graph KB-first gate stays in **③**.
- **Index two content kinds in v1: `source` and `fact`.** Evidence spans are substrings of
  `run_source.text`, so searching `source` already covers them — separate evidence indexing is
  deferred (an easy later add).
- **Index maintenance by SQLite triggers + a one-time backfill migration** (correct-by-
  construction). Facts are written/updated from several paths (ingest, recompute, the
  qualifier-resolver, backfill); triggers keep the index in sync regardless of which path wrote the
  row, so it cannot drift. Rejected: Python write-path hooks (every current/future write site must
  remember to call them — exactly the drift risk the multiple fact-writers create).
- **Freshness/trust ride along on every hit but are never filtered at the index.** `as_of`,
  `lifecycle`, `admission` are returned with each fact hit; consumers (③) decide whether to prefer
  `current`/`trusted`. Keeping that policy out of the foundation is deliberate — it is ③'s seam.

## Architecture

```
write path (UNCHANGED):  fetch → run_source.text   ┐
                         extract → fact.narrative   ├──►  FTS5 read-model (trigger-synced)
                                                    ┘
read path (NEW):  search_research(query, …) ──► ranked typed Hits ──► `dossier search` CLI
```

A new query module (e.g. `factbase/search.py`), new FTS5 tables added by a migration, and a new CLI
subcommand. No node, edge, or research-loop change.

## Components

### 1. FTS5 index (read-model)

External-content FTS5 tables linked to the base tables, so the raw text is **not duplicated** in
the index:

- `fts_source` over `run_source.text` (+ `source_url`, `title` as searchable/auxiliary columns).
- `fts_fact` over a composed document of `fact.narrative` + `fact.value` + `fact.property_name`.

Added via the existing migration framework (`factbase/migrations.py`) as a **new schema version**.
(Note: the parallel `required-qualifier-resolution` branch also adds a migration — **coordinate the
version numbers at merge** so they don't collide.) The migration also **backfills** the index from
existing `run_source`/`fact` rows; it is idempotent and re-runnable.

### 2. Synchronisation (triggers)

`AFTER INSERT / UPDATE / DELETE` triggers on `run_source` and `fact` keep the FTS tables current,
including the soft-delete path (`fact.soft_deleted_at` set → row drops out of `fts_fact`). Because
SQLite maintains the index, no Python writer can forget to update it. A `dossier reindex` command
is provided as an escape hatch to rebuild from scratch if ever needed (also useful in tests).

### 3. Query API (the artifact)

```python
search_research(
    query: str, *,
    subject: str | None = None,     # filter to one instance_key; None = global (cross-subject-ready for ④)
    kinds: list[str] = ["source", "fact"],
    limit: int = 20,
) -> list[Hit]
```

- `Hit` (typed): `kind` (`"source"|"fact"`), `ref_id` (base-row id), `subject` (instance_key),
  `snippet` (FTS5 `snippet()` highlighted passage — no manual chunking of large pages needed),
  `score` (FTS5 `bm25()`), plus kind-specific metadata:
  - `source`: `source_url`, `title`, `run_id`, `retrieved_at`.
  - `fact`: `property_name`, `as_of`, `lifecycle`, `admission`, `source_id`.
- Results across both kinds are merged and ranked by `bm25()`; `subject`/`kinds` are pre-filters.
- **Vector-ready:** the engine lives behind this signature; a later semantic adapter returns the
  same `Hit` shape, so callers (②/③) never change.

*Exact base-table column names (e.g. `run_source` title/url/timestamp columns, the subject↔run join
for a source's `instance_key`) are confirmed during plan/TDD against `schema.py`/`store.py`; the
contract above is the design intent.*

### 4. CLI (`dossier search`)

`dossier search "<query>" [--subject estonia] [--kind source|fact] [--limit N]` → a readable ranked
list: kind · subject · score · snippet · freshness (`lifecycle`/`admission` for facts). A thin
wrapper over `search_research`; no logic of its own.

## Error handling

- **Read-only, best-effort.** A malformed FTS query (stray `"`, bare operator) is sanitised, never
  raised; an empty result set is a normal answer, not an error.
- **Backfill migration is idempotent** and re-runnable; `dossier reindex` rebuilds safely.
- Trigger failures cannot silently corrupt base data — triggers only write the derived FTS tables;
  the worst case is a stale index entry, recoverable via `reindex`.

## Testing (TDD)

- **Relevance:** seed two subjects' sources + facts → `search_research("ROCA")` returns the Estonia
  `source` hit with a highlighted snippet; `subject="germany"` excludes it; `kinds=["fact"]`
  returns only fact hits.
- **Metadata:** a `fact` hit carries `as_of` / `lifecycle` / `admission`; a `source` hit carries
  `source_url` / `retrieved_at`.
- **Sync (the correctness core):** INSERT a fact → searchable; UPDATE its `narrative` (simulating
  the resolver/recompute) → new text searchable, old text gone; soft-delete → drops out; same for
  `run_source`.
- **Backfill:** migration indexes pre-existing rows; running it twice is a no-op (idempotent).
- **Robustness:** a malformed query string returns `[]`, does not raise.
- **CLI:** `dossier search` returns ranked output end-to-end over a seeded DB.

## Out of scope (explicit)

- **Semantic / vector search** — interface is left open for it; not built in v1.
- **The graph KB-first gate** (research only the delta) — sub-project **③**.
- **Cross-run fetch-skip** (reuse a cached source instead of re-fetching) — sub-project **②**.
- **Cross-subject *fact* reuse** — sub-project **④**. (① makes the index global/cross-subject-ready,
  but ① ships no policy that reuses one subject's facts for another.)
- **Evidence-span as a third indexed kind** — deferred (covered transitively by `source`).
- **Re-ranking, query expansion, freshness-weighted scoring** — consumers' policy (③), not the
  index.
