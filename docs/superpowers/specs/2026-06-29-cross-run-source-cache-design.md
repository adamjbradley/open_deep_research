# Design: cross-run source cache (Research Memory ②)

**Status:** designed (brainstormed). **Sub-project ② of 4** in the "read before you write" program
(① searchable substrate is merged, PR #53). ② makes source *content* deduped and its *summary*
reused across runs. Built **on top of ①** — it re-points ①'s FTS index and subject-join onto a new
content table, so it is effectively a v2 of the storage layer ① introduced.

## Problem

A research run acquires sources by web search (Tavily/Exa), which return `raw_content` (full page
text) directly — there is **no separate runtime fetch step** (`fetch.py` is backfill-only). Each new
raw source is then summarized by an LLM (`summarize_search_results` defaults to **True**;
`utils.py` `_finalize_search`/`_summarize_one`). That summary is cached **only within a run**
(`_SUMMARY_CACHE`, keyed by `thread_id`, `utils.py:98-105`) and the raw text is stored **per run**
(`run_source`, deduped only by `(thread_id, source_url, content_hash)`, `store.py:10-21`).

Consequences, confirmed on the live DB (research_results.db, 805 sources):
- **59 URLs are stored more than once** across runs; several share an identical `content_hash`
  (byte-identical content) yet are **re-summarized from scratch every run** — the dominant per-source
  LLM cost, re-paid needlessly.
- ①'s `fts_source` indexes `run_source`, so those duplicate rows surface as **duplicate search hits**
  (observed in the ① demo).

**What ② can and cannot save:** the web *search* call is unavoidable (the URL is unknown until
search returns, and search returns the text for free). ② eliminates the **re-summarization** and the
**duplicate storage/search-hits** — not search-API calls.

## Goal

Deduplicate source content by `content_hash` across runs, reuse summaries (skip the LLM call for
content already summarized in any prior run), and eliminate duplicate search hits — via a single new
`source_content` table that is **both** the deduped content store and the summary cache. Reuse is
keyed by `content_hash`, so it is always correct (identical bytes → identical summary) and needs
**no TTL** — changed content gets a new hash and is re-summarized automatically.

## Decisions (from brainstorming)

- **Approach 1 — `source_content` joined by `content_hash`** (chosen over a query-layer patch or a
  fully-normalized capture table). `run_source` already carries `content_hash`, so captures join to
  the content store with **no new foreign key**.
- **One unified table** for dedup + summary cache. The summary lives on the content row, computed
  once per unique content.
- **`content_hash` is the cache key and the freshness signal.** No TTL.
- **Phased delivery (A→B).** Phase A ships the summary-cost win without touching ①'s index; Phase B
  re-points the index + subject join and reclaims duplicated text storage.
- **Move `run_source.text` into `source_content`** (Phase B): the text is preserved on the content
  row, so this is a move, not a loss; new captures stop writing `run_source.text`.
- **Multi-subject sources return one hit.** A page captured in runs about different countries is one
  `source_content` row; `--subject X` matches if *any* capture resolves to X. (Subject resolution
  itself is unchanged from ①; the prompt-sentence-name fix is the separate Polish sub-project.)

## Architecture

```
search (unavoidable) → raw_content ─┐
                                     ├─► source_content (1 row per content_hash: text + summary)  ◄── fts_source (re-pointed)
run capture (thread/run) ────────────┘            ▲
                                       run_source captures ── content_hash join ── subjects (via research_runs)
summarize: reuse source_content.summary by content_hash, else LLM then store
```

### Data model
```
source_content  (NEW — deduped content + summary cache)
    id PK
    content_hash TEXT UNIQUE      -- sha256 of raw text (existing _hash(); the dedup key)
    source_url   TEXT             -- a representative URL for this content
    title        TEXT             -- provider title (from ①)
    text         TEXT             -- raw page text (moved here from run_source)
    summary      TEXT             -- LLM summary; NULL until summarized (the cross-run cache)
    summary_model TEXT            -- model that produced the summary (observability)
    first_seen_at TEXT
    soft_deleted_at TEXT

run_source  (repurposed → per-run capture / provenance; existing columns kept)
    id, thread_id, run_id, source_url, content_hash, capture_status,
    retrieved_at, soft_deleted_at
    -- `text` and `title` no longer written on new captures (authoritative copy in source_content)
```
Only `capture_status='raw_text'` captures (those with non-empty raw text) get a `source_content`
row; summarized/skipped captures with no raw text do not (avoids collapsing all empty-text captures
onto the sha256-of-empty hash).

## Components

### 1. Capture path — `RunSourceStore.record` / `record_search_sources`
For a raw-text capture: `INSERT OR IGNORE INTO source_content(content_hash, source_url, title, text,
first_seen_at)` (dedup — no-op if the content already exists), then insert a `run_source` capture row
keyed by `(thread_id, content_hash)` (a thread capturing the same content twice = one row),
**without** writing the text. The `record(...)` signature keeps its `title` parameter (① ); `title`
is routed to `source_content`, not `run_source`.

### 2. Summary cache — `utils.py` `_finalize_search` / `_summarize_one`
Two layers: keep the in-memory per-run `_SUMMARY_CACHE` (L1, thread-keyed, `utils.py:98-105`). Add
L2 = `source_content.summary` keyed by `content_hash` (cross-run, persistent). In `_summarize_one`,
after computing `content_hash` of `raw_content`:
1. L1 hit → reuse.
2. Else L2: `SELECT summary FROM source_content WHERE content_hash=?` — non-null → reuse, populate
   L1, **skip the LLM call**.
3. Else summarize (`summarize_webpage`), then `UPDATE source_content SET summary=?, summary_model=?
   WHERE content_hash=?`, populate L1.
Because `record_search_sources` runs before summarization, the `source_content` row already exists
when `_summarize_one` updates its summary. The summarize path is given the DB connection/store
(currently `_finalize_search` already holds the `run_source_store`).

### 3. ①'s index + subject join, re-pointed (Phase B)
`fts_source` becomes external-content over **`source_content(text, source_url, title)`**
(`content_rowid=source_content.id`) instead of `run_source` — one row per unique content, so
duplicate hits vanish. Its INSERT/UPDATE/DELETE triggers move to `source_content`
(`search_schema.py` `_SEARCH_SCHEMA`). Because the AFTER-UPDATE trigger fires on any column change,
a `summary` write re-syncs the index (delete+insert) even though indexed columns
(`text`/`source_url`/`title`) are unchanged — a cheap, acceptable re-sync (summary is written once
per content). `search.py` `_source_hits` joins `fts_source → source_content`, and for subject resolves
`source_content.content_hash → run_source captures → research_runs(thread_id) → subjects`, returning
one hit per content; `--subject X` filters to contents with a capture resolving to X.

### 4. `evidence` — unchanged
`evidence.run_source_id` still references a capture row; the full source text is reachable via that
row's `content_hash → source_content.text`. No FK change; span verification (`quoted_span`)
unaffected.

## Migration & phasing

**Phase A — cost win (no ① change).** Migration **v13**: create `source_content`. Capture path
upserts it; summary cache (L2) wired in. `fts_source`/subject-join still on `run_source`. Ships the
re-summarization savings and deduped content storage on their own.

**Phase B — dedup hits + storage reclaim.** Migration **v14** (+ a search-schema re-point):
populate `source_content` from existing `run_source` (`GROUP BY content_hash`, taking a representative
url/title/text and `MIN(retrieved_at)` as `first_seen_at`); re-point `fts_source` + triggers to
`source_content` (drop the old run_source-based virtual table + triggers, create the new ones via
`executescript`, then rebuild); null `run_source.text` (preserved in `source_content`). Idempotent;
`dossier reindex` rebuilds.

Migration numbers: ① took **v12**, qualifier-resolution **v11**, so ② starts at **v13** (Phase A) and
**v14** (Phase B). Re-confirm numbering at implementation time against `schema.STEPS` (currently ends
at v12).

## Error handling

- Best-effort: a summary-cache lookup/store failure or a dedup-insert race never fails a run
  (mirrors today's `record`/`summarize` error posture).
- `INSERT OR IGNORE` + the `content_hash UNIQUE` constraint make the dedup insert race-safe.
- The Phase B migration is idempotent and re-runnable; `dossier reindex` recovers the index.

## Testing (TDD; injected `model_call` for the summary step)

- **Dedup:** two captures (different threads) of identical content → **one** `source_content` row,
  **two** `run_source` capture rows; capturing the same content twice in one thread → one capture row.
- **Summary reuse:** run 1 summarizes content C (model called once); run 2 sees the same
  `content_hash` → **model NOT called**, summary reused (assert via a counting/injected `model_call`).
- **Changed content:** different `content_hash` → new `source_content` row, summary re-computed.
- **No row for empty captures:** a `summarized`/`skipped` capture with no raw text creates no
  `source_content` row.
- **FTS dedup (Phase B):** content indexed once → `search_research` returns a single hit per content.
- **Subject via captures (Phase B):** a content captured under subject EST is returned for
  `--subject Estonia`; a content captured under two subjects returns one hit matching either.
- **Migration:** Phase B backfill dedups existing rows and is idempotent (running twice is a no-op);
  `evidence.run_source_id` rows still resolve to text via `content_hash`.

## Out of scope

- Reducing search-API calls (the search is unavoidable; only summarize/storage are reused).
- Time-based TTL refresh (content_hash already captures change).
- The source→subject resolution fix for prompt-sentence `subjects.name` values (the **Polish**
  sub-project).
- Cross-subject *fact* reuse (sub-project ④) and the KB-first research gate (sub-project ③).
