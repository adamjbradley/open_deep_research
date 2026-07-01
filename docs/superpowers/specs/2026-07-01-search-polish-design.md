# Design: search read-layer polish

**Status:** designed (brainstormed). The final sub-project of the "read before you write" program
(①②③ merged: PRs #53/#54/#55). A bundle of four contained, read-only fixes to the search layer —
the loose ends surfaced by real-data testing and the ①/② reviews. No schema change, no migration.

## Problem

Four known defects in `factbase/search.py`, all read-layer:

1. **Empty snippet renders as `None`.** A `fact` hit's snippet is `snippet(fts_fact, 0, …)` over the
   `narrative` column; when the matched fact has a `NULL` narrative (the hit matched `value`/
   `property_name`), the snippet is empty and `dossier search` prints `None`. (Observed live on the
   real DB — most facts have no narrative.)
2. **Source `--subject` resolution is sparse.** `_source_hits` resolves a source's subject via
   `CountryResolver().resolve(subjects.name)`, but batch runs stored `subjects.name` as full prompt
   sentences ("Research Afghanistan for the 'country_cbdc' profile…"), which don't resolve — so
   `--subject` over sources returns nothing even when the sources exist.
3. **Cross-kind ranking is only approximate.** Merged `source`+`fact` results sort by raw `−bm25`,
   which is table-relative across two FTS tables, so a weaker hit from one table can outrank a
   stronger hit from the other (the ① review's Medium; shipped with an "approximately comparable"
   caveat).
4. **`conn.row_factory` mutated, not restored.** `_source_hits`/`_fact_hits`/`RunSourceStore.read`
   set `conn.row_factory = aiosqlite.Row` on the shared connection and never restore it — a latent
   hazard flagged in ①/② (harmless today with per-command connections, a real bug once a pooled/
   shared connection is introduced).

## Goal

Fix all four in `search.py` (+ tests), read-only and behavior-scoped: fact hits show a real value,
source `--subject` resolves from prompt-sentence names, the merged list ranks by a normalized
cross-kind score, and searches leave `conn.row_factory` unchanged.

## Decisions (from brainstorming — all four in scope)

- **Snippet fallback:** `_fact_hits` also selects `f.value`; a new `value` field on `Hit`. When a
  hit's `snippet` is empty, `format_hits` renders a fallback (`"<property_name> = <value>"` for a
  fact; `source_url` for a source).
- **Source subject via `resolve_in_text`:** `_source_hits` resolves the subject with
  `CountryResolver().resolve_in_text(subjects.name)` (scans the text for a country) instead of
  `resolve(...)`. It is a superset — a clean name still resolves — so no data migration. Fixing the
  batch runner to store clean `subjects.name` is a separate future data-quality item (out of scope).
- **Min-max cross-kind normalization:** in `search_research`, after collecting hits, normalize each
  *kind's* raw scores to `[0,1]` (min-max within that kind's result set; all-equal or single hit →
  `1.0`), set `Hit.score` to the normalized value, then merge and sort. `Hit.score` becomes a
  normalized `[0,1]` cross-kind rank (documented).
- **Save/restore `row_factory`:** each query saves the connection's prior `row_factory`, sets
  `aiosqlite.Row`, and restores it in a `finally`, so a search never leaves the connection mutated.

## Architecture

All changes are in `src/open_deep_research/factbase/search.py` (the `Hit` dataclass, `_fact_hits`,
`_source_hits`, `search_research`, `format_hits`) and `RunSourceStore.read` in `factbase/store.py`
(the `row_factory` restore). Read-only; nothing else touched.

## Components

### 1. `Hit.value` + snippet fallback
Add `value: str | None = None` to `Hit`. `_fact_hits` adds `f.value` to its SELECT and sets it on the
hit. `format_hits`: `snip = h.snippet or _fallback(h)` where `_fallback` = `f"{h.property_name} = {h.value}"`
for a `fact`, else `h.source_url or "(no snippet)"`. (Sources almost always have a snippet.)

### 2. Source subject resolution
In `_source_hits`, replace `CountryResolver().resolve(row["subject_name"])` with
`CountryResolver().resolve_in_text(row["subject_name"])`. Unresolvable → `None` (excluded under a
`--subject` filter, present globally) — unchanged semantics, just a better resolver.

### 3. Cross-kind score normalization
Factor a helper `_normalize(hits)` that min-max scales a list's `.score` in place to `[0,1]`
(`hi==lo → all 1.0`). In `search_research`, apply it **per kind** (source hits, fact hits) before
concatenating, then sort the merged list by `score` descending and take `limit`. Update the module's
"approximately comparable" note to "normalized per-kind to [0,1] before merge".

### 4. `row_factory` restore
In `_source_hits`, `_fact_hits`, and `RunSourceStore.read`: `prev = conn.row_factory; conn.row_factory
= aiosqlite.Row; try: <query> finally: conn.row_factory = prev`.

## Error handling
- Read-only, best-effort as today: a malformed query still returns `[]`; the fallback/normalization/
  restore add no new failure modes.
- Normalization guards `max == min` (avoid divide-by-zero → all `1.0`) and empty lists.

## Testing (TDD)
- **Snippet fallback:** a fact hit whose matched fact has a `NULL` narrative renders `"<property> =
  <value>"`, not `None`.
- **Source subject:** a source whose `subjects.name` is a prompt sentence ("Research Estonia for …")
  is returned under `--subject Estonia` (resolves to `EST` via `resolve_in_text`) and excluded under
  `--subject Germany`.
- **Normalization:** given a source hit and a fact hit with different raw bm25, both scores land in
  `[0,1]`; a single-hit or all-equal kind normalizes to `1.0`; the merged list is sorted by the
  normalized score.
- **`row_factory`:** `conn.row_factory` is unchanged (equals its prior value) after `search_research`
  and after `read()`.

## Out of scope
- Fixing the batch runner to store clean `subjects.name` (a data-quality change at the write path).
- Semantic/embedding ranking (the vector-adapter path left open by ①).
- Any schema change (this is purely read-layer).
