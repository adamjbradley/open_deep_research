# deep_researcher — reliability, quality & efficiency hardening — design

**Date:** 2026-06-19
**Status:** Approved (brainstorming) → implementation plan next
**Scope owner branch:** harden-routing-failover (or a fresh branch)

## Context & goal

`deep_researcher` (a LangGraph agent in `src/open_deep_research/deep_researcher.py`) researches a
**country** against a structured **profile** (properties like `foundational_id_scheme`,
`legal_basis`, `id_coverage_pct`), extracts sourced/dated structured **facts**, and persists a
per-country **dossier** to SQLite. It runs in **batches** across many countries via
`factbase/batch.py` (`BatchRunner` + `default_run_one`) driven by the `dossier batch` CLI.

**The goal this design serves:** reliable, comprehensive, accurate per-country dossiers, runnable
in batches — with no silent failures, good property coverage, high fact recall, and efficient use
of LLM calls / rate-limited backends.

## The systemic problem

The pipeline has **no notion of "this run produced nothing."** Success and failure are
indistinguishable at every handoff, and the batch ledger marks a completed-but-empty run as
**done** — permanently (resume never re-selects `done`). The Brazil failure (country marked
`done` with 0 sources / 0 notes / 0 researched facts) is the visible symptom.

Ground truth (Brazil run `research_runs.id=441` stored config): the batch runs in
**facts-first mode** (`facts_first_mode:true, whole_profile_mode:false`), forced by
`FACTS_FIRST_MODE=true` in `.env` (read with top precedence by
`Configuration.from_runnable_config`, configuration.py:445-466). So the live path is:
`write_research_brief → research_supervisor → extract_facts → assess_sufficiency →
answer_from_facts → persist` with at most **one** gap round (`max_fact_rounds=2`).

## Decisions (resolved)

1. **Research thoroughness:** batches use **whole_profile_mode** (up to 5 *targeted* gap rounds on
   only-missing properties + an affirmative-absence pass), not facts-first.
2. **Reliability gate:** a *truly empty* run (0 researched facts **and** 0 raw_text sources
   gathered) is marked so the **next batch resume auto-retries it** (self-healing); a per-item
   attempt counter gives visibility. A *thin* run (sources gathered, few facts) is **surfaced in
   the summary, not auto-failed** — it may be a legitimately sparse country, and re-running it
   would loop without new information.
3. **Summarization:** **off by default** in facts/whole-profile modes (extraction reads raw text,
   so summaries are bypassed there).

**Design principles:** each unit is independently shippable and unit-testable; reliability before
efficiency before coverage; no silent drops anywhere (log/surface what is skipped).

---

## Unit A — Reliability gate (no silent-empty dossiers)  *[highest priority]*

**Findings addressed:** empty research silently accepted; 0-fact run marked `done` and permanently
skipped on resume.

**Evidence:**
- `supervisor_tools` (deep_researcher.py:~525-577): `no_tool_calls = not most_recent_message.tool_calls`;
  the exit `if exceeded_allowed_iterations or no_tool_calls or research_complete_tool_call: → END`
  fires on a **blank supervisor turn** with no corrective nudge and without writing `raw_notes`.
  The premature-completion guard (lines ~543-567) only handles `ResearchComplete`. An NVIDIA model
  (minimax-m3) that returns text with no tool call hits this path → empty research (the Brazil cause).
- `extract_facts` (deep_researcher.py:~1674-1676): 0 sources → `return {}` silently.
- `persist_research` (deep_researcher.py:~1338): only inspects `final_report` text via
  `_report_is_failed`; never checks fact/source count → saves `status="completed"`.
- `batch.py:~68-72`: `led.mark(key, status="done")` fires on any non-raising `run_one` return;
  `batch_ledger.pending_items` (batch_ledger.py:~74-82) excludes `done` → permanent.

**Changes:**
- **A1** `supervisor_tools`: add a corrective-nudge branch for `no_tool_calls AND not
  conducted_research AND not exceeded_allowed_iterations` — answer with a nudge ("you have not
  dispatched any ConductResearch; do so now") and loop back to `supervisor`, mirroring the
  `ResearchComplete` guard. The iteration cap still bounds the loop.
- **A2** Surface counts: `extract_facts` returns `fact_count` (len of kept records) and
  `raw_text_source_count`; `persist_research` writes them to the `research_runs` row and the final
  graph state.
- **A3** `persist_research`: when `fact_count == 0 AND raw_text_source_count == 0` (an empty run),
  persist `status="error"` (reuse the existing error-status path) instead of `"completed"`.
  (Population-only counts as empty — see Unit-wide note: population is statically loaded, not
  researched, so exclude it from the fact-count floor.)
- **A4** `batch.py` worker + `default_run_one`: `default_run_one` returns a dict
  `{"report_id", "fact_count", "status"}` (back-compat: `BatchRunner` reads `.get`). The worker
  marks `status="failed"` (not `done`) when the run is empty/error, so resume auto-retries; it
  increments a `batch_item.attempt_count`. Surface attempt_count + thin countries in the batch
  summary.

**Interface change:** `default_run_one` return type str → dict. Update `BatchRunner.worker`
(batch.py:~63-72) and any caller of the return value.

**Tests (offline):**
- A1: fake supervisor model returns an AIMessage with empty `tool_calls` on turn 1 → assert a
  corrective ToolMessage is appended and control loops to `supervisor` (not END), and that after a
  subsequent ConductResearch it proceeds.
- A3: `persist_research` with 0 facts / 0 sources → assert `status="error"` persisted.
- A4: in-memory ledger; `run_one` returns `{fact_count:0,...}` → assert item marked `failed`,
  `attempt_count` incremented, and `pending_items` re-selects it.

**Acceptance:** a run that gathers nothing is never marked `done`; the next resume re-runs it. A
blank supervisor turn no longer ends research empty.

---

## Unit B — Comprehensive coverage (whole-profile mode)

**Findings addressed:** batch under-researches (facts-first, 1 gap round) instead of using the
purpose-built whole-profile completeness loop.

**Evidence:** `route_after_extract` (deep_researcher.py:~2207-2214) → `assess_completeness` only
when `whole_profile_mode`; `assess_completeness` (deep_researcher.py:~1783-1856) loops up to
`max_profile_rounds=6`, narrows `target_properties` to incomplete (line ~1851), runs an
affirmative-absence pass. `FACTS_FIRST_MODE=true` in `.env` currently forces facts-first globally.

**Changes:**
- **B1** `default_run_one` configurable: add `"whole_profile_mode": True` (and an explicit
  `"max_profile_rounds"`). Because no `WHOLE_PROFILE_MODE` env is set, the configurable takes
  effect; whole-profile wins over facts-first in routing.
- **B2** Reconcile the global `.env` `FACTS_FIRST_MODE=true`: decide intended default. Recommended:
  remove it from `.env` (so single queries default to report/answer per request) and rely on
  explicit per-run mode flags; keep facts-first available via config. Document in `.env.example`.

**Tests:** integration smoke (mocked models) asserting `route_after_extract` →
`assess_completeness` for a `default_run_one`-shaped config; a unit check that whole_profile wins
when both flags set.

**Acceptance:** batch runs traverse the completeness loop with targeted gap rounds; a country with
partial coverage triggers additional targeted research up to the cap.

---

## Unit C — Extraction recall (stop dropping correct facts)

**Findings addressed:** ~20-40% of model-extracted facts silently dropped by strict span
verification; long sources truncated; extractor-model asymmetry.

**Evidence:**
- `extractor._norm` (extractor.py:~16): only `whitespace-collapse + lower()`; **no Unicode
  normalization**. Span gate (extractor.py:~30): `_norm(span) not in norm_source` (verbatim
  substring) → paraphrased / cross-sentence / non-breaking-space / curly-quote / en-dash spans
  dropped, all-or-nothing per fact.
- `prompting._SOURCE_CAP = 8000` (prompting.py:~10): facts beyond 8000 chars never seen.
- Only the `gemini` preset routes `extract_facts` to a strong extractor
  (model_routing.json step_overrides); others fall back to `researcher_model`.

**Changes:**
- **C1** `_norm`: `unicodedata.normalize("NFKD", s)` + map non-breaking spaces to spaces before
  whitespace-collapse/lower.
- **C2** Fuzzy fallback: if the verbatim substring check fails, accept the span when
  `difflib.SequenceMatcher` ratio against the best-matching source window ≥ a conservative
  threshold (~0.9). The span must still be substantially present → no hallucinated facts admitted.
  Keep it cheap (only on substring-miss; window-scan bounded).
- **C3** Raise `_SOURCE_CAP` (e.g. 8000 → 24000) and/or chunk long sources into multiple extraction
  calls; measure cost impact.
- **C4** Give every routing preset (esp. `nvidia`) a strong `extract_facts` step-override aimed at
  recall, not just tool-call validity.

**Tests (offline):**
- C1: `_norm` collapses NBSP/curly-quote/en-dash so a source with those + a clean span matches.
- C2: a paraphrased span that fails substring but is ~0.92 similar is accepted; a 0.4-similar
  (hallucinated) span is rejected. Reuse the real `extractor.extract` with a fake `model_call`.
- C3: a fact located at char ~12000 of a source is extracted after the cap raise.

**Acceptance:** facts present in sources but previously dropped (whitespace/quote/paraphrase) are
now kept; measured recall up on a sample (e.g. re-extract Brazil/India sources, compare counts).

---

## Unit D — Efficiency & rate-limits

**Findings addressed:** per-source summarization dominates LLM cost and is bypassed by extraction;
`extract_facts` fires an unbounded `asyncio.gather` → 429 cascades + circuit-breaker fan-out gap.

**Evidence:**
- Summarization: `tavily_search` → `_summarize_one` (utils.py), gated by
  `summarize_search_results` (default True). Extraction reads raw_text from `run_source`
  (deep_researcher.py:~1669-1673), bypassing summaries; in facts/whole-profile modes the prose
  report is skipped entirely.
- `extract_facts` (deep_researcher.py:~1690-1691): `await asyncio.gather(*[_extract_one(s) ...])`
  with **no semaphore**; each failure returns `[]` silently. Observed live in the Brazil re-run:
  37 failovers / 31 circuit-breaks — minimax-m3 hit simultaneously by ~20+ concurrent sources, the
  circuit-breaker mark-down not reaching in-flight peers.

**Changes:**
- **D1** Default `summarize_search_results=False` when `facts_first_mode or whole_profile_mode`
  (mode-linked default in `Configuration` or `default_run_one`). Researcher reasons on raw search
  snippets. Document the tradeoff.
- **D2** Bound `extract_facts` concurrency with an `asyncio.Semaphore` (size = a new
  `extract_facts_concurrency` config, default ~4, or `max_concurrent_research_units`). Additionally,
  re-check the failover down-tracker per attempt in the `configurable_model.ainvoke` loop (or guard
  in `_extract_one`) so a model marked down by an earlier in-flight call is skipped by peers —
  closing the circuit-breaker fan-out gap. Do not silently swallow per-source extraction failures:
  count them and surface a `extraction_errors` number.

**Tests:**
- D1: with mode flags set and no explicit override, assert `Configuration.summarize_search_results`
  resolves False; with explicit True it stays True.
- D2: a fake model that 429s then succeeds; assert no more than N concurrent calls; assert a
  marked-down model is skipped by subsequent concurrent tasks (extend the failover integration
  tests). Assert per-source errors are counted, not swallowed.

**Acceptance:** a facts/dossier run makes ~order-of-magnitude fewer LLM calls; `extract_facts` over
20+ sources no longer produces a 429 storm; a throttled model is abandoned once, not per-source.

---

## Unit E — Handoff correctness (small)

**Findings addressed:** facts-first gap round re-targets all properties; DB error treated as
"sufficient."

**Evidence:** `assess_sufficiency` (deep_researcher.py:~1743-1780) sets `missing_information` but
not `target_properties` on the gap update (line ~1774-1776) — `write_research_brief` then re-reads
the round-0 full list. The except handler (line ~1765-1766) logs "treating as sufficient" on a DB
error and proceeds.

**Changes:**
- **E1** `assess_sufficiency`: include `"target_properties": missing` in the gap update (mirror
  `assess_completeness` line ~1851).
- **E2** `assess_sufficiency` except handler: on error, route as "still missing" (loop one more
  round) rather than "sufficient," up to the cap.

**Tests (offline):** E1: after a gap decision, assert state `target_properties == missing` subset.
E2: a raising fact-base lookup → assert it loops (within cap) instead of answering.

**Acceptance:** facts-first gap rounds research only missing properties; a transient DB blip does
not prematurely finalize a thin dossier.

---

## Sequencing & risk

**Order:** A → D → C → B → E. Reliability first (stops permanent silent-empty); then D's cheap
wins also de-risk the heavier whole-profile rounds (fewer calls, no 429 storms); then C lifts
recall; then B turns on the thorough mode; then E's small fixes.

**Risks:**
- C2 fuzzy threshold too low → admits wrong facts. Mitigate: conservative (~0.9), and the value
  validator (`PropertyDef.validate`) still gates.
- B + raised rounds → higher cost/time per country. Mitigate: D1/D2 cut per-round cost; the
  auto-retry + attempt cap (A4) prevents infinite re-research of un-researchable countries.
- A4 changes `default_run_one`'s return type — update all callers.

## Out of scope

Re-architecting the supervisor/researcher subgraphs; replacing Tavily; a UI; changing the
fact/registry schemas; the model-routing presets beyond C4's extract_facts override.

## Validation evidence (live)

The Brazil re-run with the supervisor on a reliable dispatcher (`agy:claude-opus-4.6` →
`claude-opus-4-8`) went from **0 → 32 researched facts**: new run `research_runs.id=443` gathered
**54 sources** and **97 KB of raw_notes** (vs 0 sources / empty `[]` before), now covering
`foundational_id_scheme` (12), `scheme_status` (8), `legal_basis` (7), `biometric_capture` (5).
This is direct confirmation that the supervisor-dispatch fix behind **Unit A1** resolves the
empty-research case. `id_coverage_pct` and `data_protection_law` remain missing — a coverage / recall
concern that **Units B (more targeted rounds)** and **C (span-verification recall)** target. The
same re-run was bottlenecked for ~1h in throttled extraction (37 failovers / 31 circuit-breaks),
demonstrating **Unit D2**'s necessity firsthand.
