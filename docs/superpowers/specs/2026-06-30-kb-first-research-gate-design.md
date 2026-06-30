# Design: KB-first research gate (Research Memory ③)

**Status:** designed (brainstormed). **Sub-project ③ of 4** in the "read before you write" program
(① searchable substrate + ② cross-run source cache are merged: PRs #53, #54). ③ makes a run consult
the fact base *before* spending web search, and research only the genuine delta. It is the policy
seam ① deliberately left open (freshness/trust "ride along but are never filtered at the index —
that's ③'s job").

## Problem

The research loop is **already cross-run aware**, but blind on round 1:

- The gap loops read facts by `instance_key`, **not** `run_id` (`factbase/query.py:_rows` filters only
  `soft_deleted_at IS NULL AND instance_key = ?`), so `assess_completeness` / `assess_sufficiency`
  (`nodes/completeness.py`) already see prior-run facts and already exclude already-`resolved`
  properties from the next gap round's `target_properties`.
- **But round 1 researches the whole profile blind.** `write_research_brief` defaults round-1
  `target_properties` to all profile properties (whole-profile) or `resolve_target_properties(...)`
  (facts-first) *before any completeness check has run* (`nodes/brief.py:~322-337`). A subject whose
  properties are already trusted and recent still pays for a full round-1 web-search fan-out.

The existing reuse-before-research gate, `assess_knowledge` (`nodes/brief.py:111-182`), only covers
**prose mode**: it asks an LLM whether the prose `subjects.current_report` already answers the
question and, if so, routes to `answer_from_dossier`. It does not consult the **structured fact
base**, and it does not run in facts-first / whole-profile modes.

## Goal

A **pre-loop gate** that, for a subject with prior facts, subtracts the already-*good* properties
from round-1 research and — if everything is good — skips research entirely. "Good enough to skip"
is a **conservative** predicate: trusted, unconflicted, and recently captured.

## Decisions (from brainstorming)

- **Conservative reuse predicate** (chosen over "any present value" and "per-property windows"): a
  property is reusable iff its current grouped value is `admission='trusted'`, **not** `in_conflict`,
  **and** captured within a freshness window. Provisional / conflicted / stale → re-researched.
- **Freshness keys on capture time, not subject-matter date.** The gate uses `fact.created_at`
  (when we last captured/verified the value) — surfaced as `captured_at = MAX(created_at)` per group
  — not `as_of` (the data's as-of date). The gate's question is "did we verify this recently enough
  to trust it without re-checking?"
- **Default window = 180 days**, configurable: `Configuration.kb_reuse_max_age_days` (default 180).
- **Gate lives in `assess_knowledge`** (the smallest seam — already runs once at entry, resolves the
  subject, gated by `use_knowledge_base`). Target-property resolution is factored into a shared
  helper so the gate and `write_research_brief` don't duplicate it.
- **All-good → `answer_from_facts`** (skip research entirely); **partial → `write_research_brief`**
  with `target_properties = to_research` + a `missing_information` gap. Reuses the exact state
  contract the gap loops already emit.
- **Scope: facts-first + whole-profile modes only.** Prose mode keeps its existing LLM dossier
  cache-hit unchanged. New flag `Configuration.kb_first_gate` (off → today's behavior).
- **`lifecycle` stays dormant.** Freshness is computed on the fly from `created_at`; ③ does not
  transition facts to `stale`/`superseded` (a future enhancement).
- **Best-effort:** any failure reading/classifying the KB falls through to normal research (research
  everything), never blocks a run.

## Architecture

```
START → preallocate_run → assess_knowledge ──┐
                                             ├─ all target props good → answer_from_facts → persist   (skip research)
                                             ├─ some good → write_research_brief(target_properties=to_research, missing_information=gap)
                                             └─ KB off / new subject / read error → write_research_brief (whole profile, today's path)
```

The gate runs once at entry. From round 2 onward, the existing gap loops continue to narrow
`target_properties` exactly as today — ③ only fixes round 1.

## Components

### 1. Reuse predicate
A pure function over a grouped fact row + clock:
```
is_reusable(group_row, *, now, max_age_days) -> bool
    = group_row.admission == "trusted"
      and not group_row.in_conflict
      and captured_within(group_row.captured_at, now, max_age_days)
```
`captured_within` parses the ISO `captured_at` and returns False on missing/unparseable timestamps
(fail safe → re-research). Pure and unit-testable.

### 2. Surface `captured_at` on grouped rows
`FactQuery.show_grouped` (`factbase/query.py`) currently carries `admission`, `in_conflict`, `as_of`,
`source_count`. Add `captured_at = MAX(created_at)` per canonical group (the most recent capture of
that value), so the predicate has a freshness timestamp. Small, additive query change; existing
consumers ignore the new key.

### 3. The gate, in `assess_knowledge`
When `use_knowledge_base` **and** `kb_first_gate` **and** (`facts_first_mode` or `whole_profile_mode`):
1. Resolve the run's **target properties** via a shared helper `resolve_run_target_properties(state,
   config, conn)` — extracted from the logic currently inline in `write_research_brief:~322-337`
   (whole-profile = all `required` profile props; facts-first = `resolve_target_properties(question,
   …)`). `write_research_brief` is refactored to call the same helper (DRY; no behavior change).
2. Read `FactQuery.show_grouped(instance_key)`; for each target property, apply `is_reusable`.
3. Split into `reusable` and `to_research`.
4. **If `to_research` is empty** → `Command(goto="answer_from_facts")` (skip research). **Else** →
   `Command(goto="write_research_brief", update={"target_properties": to_research,
   "missing_information": <gap naming to_research>, "kb_prefiltered": True})`.

`kb_prefiltered` tells `write_research_brief` round-1 to honor the pre-narrowed `target_properties`
instead of re-expanding to the whole profile. (Prose mode and the non-facts paths are untouched.)

### 4. `answer_from_facts` reachability
`answer_from_facts` already exists as a terminal-ish node (facts-first answers from the fact base).
③ adds an edge so `assess_knowledge` can route to it directly when the gate finds everything good.
Confirm at plan time whether a new conditional target must be registered on `assess_knowledge`'s
`Command` goto set.

### 5. Configuration
- `kb_first_gate: bool = False` — master switch for ③ (off → today's behavior).
- `kb_reuse_max_age_days: int = 180` — freshness window for the predicate.

## Error handling

- KB read / resolver / predicate failure inside the gate → log at warning, fall through to the
  normal `write_research_brief` whole-profile path (never block; never skip research on an error).
- A subject that doesn't resolve to an `instance_key`, or has no prior facts → no reusable props →
  normal full research (the gate is a no-op).
- `captured_at` missing/unparseable for a group → that property is treated as **not** reusable
  (re-researched), the safe default.

## Testing (TDD)

- **Predicate:** trusted + recent + unconflicted → reusable; provisional → not; trusted but older
  than the window → not; trusted + recent but `in_conflict` → not; missing `captured_at` → not.
- **`captured_at`:** `show_grouped` returns `MAX(created_at)` per group.
- **Gate — partial:** a subject with 2 of 4 target props trusted+recent → `write_research_brief`
  receives `target_properties` = the other 2 + `kb_prefiltered=True`; round 1 researches only those.
- **Gate — all good:** all target props trusted+recent → route to `answer_from_facts`, no research.
- **Gate — off / new subject:** `kb_first_gate=False` or no prior facts → unchanged whole-profile
  round 1.
- **Best-effort:** a forced KB-read error → falls through to normal research (asserted, no raise).
- **Freshness window:** with `kb_reuse_max_age_days` set low, a recently-captured trusted fact is
  re-researched (proves the window is honored).

## Out of scope

- Per-property freshness windows (the rejected alternative; would need profile-schema `max_age`).
- Transitioning `lifecycle` to `stale`/`superseded` (freshness is computed on the fly here).
- Changing prose-mode `assess_knowledge` (its LLM dossier cache-hit is unchanged).
- Cross-subject *fact* reuse — sub-project ④.
- Keyword-recall at the gate via `search_research` — the structured `show_grouped` predicate is the
  v1 mechanism; semantic/keyword gating is a possible later refinement.
