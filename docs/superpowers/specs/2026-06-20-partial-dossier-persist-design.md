# Partial Dossier Persist (checkpoint on abort/timeout) — Design

**Date:** 2026-06-20
**Status:** Approved (design); pending implementation plan
**Origin:** Follow-up #1 from `2026-06-20-dossier-completion-followups.md`. A whole-profile
Estonia run gathered 52 facts across 5/6 properties but timed out in the gap loop and persisted
**no** subject dossier (`subjects = 0`), because `persist_research` only runs at finalize.

## Problem

The whole-profile flow is `extract_facts → assess_completeness → [write_research_brief (gap loop) |
synthesize_narrative] → persist_research → END`. The subject dossier (`subjects.current_report`)
is written **only** by `persist_research`, the terminal node. An external timeout-kill mid-loop
(the process just dies — no clean shutdown to run a finalize node) never reaches it, so a run that
gathered good facts persists nothing at the subject level. (The facts themselves ARE in the `fact`
table; only the subject record + report are missing.) Persisting a **partial dossier** beats
persisting nothing.

## Goal

Guarantee that a whole-profile run which gathered facts persists a **partial subject dossier**
even if it aborts/times out mid-loop — at **zero extra LLM cost** (so it doesn't worsen the very
timeout we're mitigating).

## Section 1 — Architecture

Factor the subject write out of `persist_research` and call it both mid-loop (cheap) and at
finalize (full narrative):

- **`_persist_subject(state, config, report, status)`** — the existing subject-resolution +
  `log_research_run(run_id=prealloc_run_id)` + subject upsert, parameterized by `report` text and
  `status`. Because it keys on `prealloc_run_id`, repeated calls **update the same run + subject**
  (no duplicate `research_runs`).
- **`_checkpoint_dossier(state, config)`** — mid-loop checkpoint: if `fact_count > 0`, assemble a
  **cheap facts-based report from the DB (no LLM)** and call `_persist_subject(..., status="partial")`.
- **Wire `_checkpoint_dossier` into `assess_completeness`** (which runs after every `extract_facts`
  round, before the loop/finalize decision). After the first extraction onward, the subject is
  always persisted with the latest facts.
- **`persist_research` (finalize) unchanged in behavior** — `synthesize_narrative → persist_research`
  writes the full narrative (`status="completed"`), upgrading the same subject/run.

**Net:** abort mid-loop → the last checkpoint's partial dossier survives (subject + facts +
facts-report, queryable via `dossier show`); completion → upgraded to the full narrative dossier.

## Section 2 — Cheap report + guards

- **Cheap report (no LLM):** reuse the fact-grouping that `dossier show` already uses
  (`group_by_canonical` / the grouped-facts renderer) to assemble a markdown facts summary straight
  from the DB — the same content as `dossier show --format md`.
- **Guard 1 — `fact_count > 0`:** the checkpoint only fires when at least one fact exists,
  preserving the empty-run gate's intent (a genuinely-empty run is still not checkpointed and is
  still rejected at finalize).
- **Guard 2 — don't poison an existing dossier:** the checkpoint sets `current_report = facts-report`
  **only when the subject has no existing report yet** (a brand-new subject — the dossier-batch case
  that failed). For an existing subject, the checkpoint keeps the run/facts linked but leaves the
  prior established dossier intact until the final narrative.
- **Status:** the checkpoint marks `status="partial"` (vs `completed` at finalize). `assess_knowledge`'s
  cache path treats a `partial` dossier as researchable (like a gap), so a later full run still
  completes it rather than serving the partial as final.

## Section 3 — Testing

**Deterministic (seed facts into a temp DB; no LLM — gate the merge):**
- `_persist_subject` idempotency — two calls with the same `prealloc_run_id` → one run + one subject
  (no duplicate `research_runs`).
- `_checkpoint_dossier` with facts → subject persisted, `current_report` non-empty + contains the
  facts, `status="partial"`.
- Guard 1 — `_checkpoint_dossier` with 0 facts → no subject persisted (`subjects = 0`).
- Guard 2 — existing subject with a `completed` report → checkpoint does NOT overwrite it.
- Wiring — `assess_completeness` (given facts) invokes `_checkpoint_dossier` before its loop/finalize
  decision (spy/monkeypatch).

**Empirical (the real proof):** re-run the Estonia whole-profile eval; confirm that even when the
gap loop churns and the process is killed at the timeout, `subjects = 1` (partial 5/6 dossier
persisted) vs `subjects = 0` today; `dossier show Estonia` renders it.

## Files touched (anticipated)

- `deep_researcher.py` — extract `_persist_subject` from `persist_research`; add
  `_checkpoint_dossier`; call it in `assess_completeness`.
- Reuse the existing grouped-facts renderer (`factbase` show/group helpers) for the cheap report.
- Tests alongside `test_knowledge_flow.py` / a new `test_partial_persist.py`.

## Non-goals

- Not synthesizing the narrative per checkpoint (explicitly rejected — would add LLM cost and worsen
  the timeout). Narrative stays at finalize only.
- Not fixing the gap-loop churn or the `data_protection_law` extraction gap (follow-ups #2 and #3 —
  separate specs).
- Not changing the empty-run gate semantics (genuinely-empty runs still rejected).

## Risks / open questions

- **`partial` status downstream:** ensure `assess_knowledge`/cache logic and the batch ledger treat
  a `partial` subject as not-yet-complete (researchable), so it isn't served as a final answer and a
  resume/re-run can finish it. Confirm during implementation.
- **Checkpoint frequency cost:** the cheap report is a DB read + format per gap round — negligible,
  but confirm the grouped-facts render on a large fact set isn't slow enough to matter.
- **Subject resolution mid-loop:** `_resolve_subject` may make an LLM call when `state["subject"]`
  isn't set. In dossier-batch mode `subject` is set up front (the country), so the checkpoint is
  LLM-free; guard the checkpoint to skip subject *resolution* (only use an already-set subject) to
  keep the no-LLM guarantee — if no subject is set yet, skip the checkpoint that round.
