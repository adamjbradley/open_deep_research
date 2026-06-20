# Gap-loop No-progress Bail-out — Design

**Date:** 2026-06-20
**Status:** Approved (design); pending implementation plan
**Origin:** Follow-up #2 from `2026-06-20-dossier-completion-followups.md`, re-scoped after reading
the actual gap-loop code.

## Problem (re-scoped)

The whole-profile gap loop in `assess_completeness` routes to a new gap round
(`write_research_brief`) while any required property is incomplete and `max_profile_rounds` allows,
and only exits on **complete** or **budget-exhausted**. There is no early exit when a gap round
makes **no progress**.

In the Estonia run, `data_protection_law` (`kind: boolean`, `required`, **not** `absence_allowed`)
could never be retired by the affirmative-absence pass (it isn't absence-allowed, and the data is
not actually absent — extraction just fails on it, follow-up #3). So the loop ran the full
`max_profile_rounds` adding **0 net-new facts**, burning the time budget.

Two things the original follow-up #2 assumed are **already handled** and are out of scope here:
- The loop already narrows `target_properties` to just the incomplete set each round.
- `write_research_brief` already builds a **gap-scoped** brief when a dossier exists — and after
  follow-up #1 (partial-persist, merged in PR #45) a partial dossier now exists each round, so the
  gap-scoped brief triggers automatically. (We add one verification test for this; no new code.)

## Goal

Stop the gap loop as soon as a gap round closes **zero** gaps, instead of burning every remaining
round — at **zero extra LLM cost**. Combined with partial-persist, a churned run then finalizes
fast *and* has a persisted partial dossier.

## Design

### The signal

A gap round made no progress if the set of still-incomplete **required** properties did not shrink.
`incomplete` can only stay-same or shrink across rounds (facts persist; a complete property never
un-completes), so `set(incomplete) == set(prev_incomplete)` means the round resolved nothing —
including the real case where extraction *attempted* a property but the facts dropped (still
incomplete → bail). This is a better measure than raw fact-count, which can rise without closing a
gap.

### The mechanic (in `assess_completeness`)

Replaces the current `if incomplete and rounds_used + 1 < max_profile_rounds:` decision:

```python
prev_incomplete = state.get("prev_incomplete_props")
no_progress = (
    rounds_used >= 1                                  # this assessment follows a gap round
    and prev_incomplete is not None
    and set(incomplete) == set(prev_incomplete)       # nothing became complete this round
)
if incomplete and not no_progress and rounds_used + 1 < configurable.max_profile_rounds:
    return Command(goto="write_research_brief", update={
        "missing_information": gap, "target_properties": incomplete,
        "fact_rounds_used": rounds_used + 1,
        "prev_incomplete_props": incomplete,          # remember for next round's compare
    })
if no_progress:
    logger.info("Gap round closed zero gaps (%s unchanged); bailing out to finalize", incomplete)
return Command(goto="synthesize_narrative", update={"fact_rounds_used": rounds_used})
```

**Threshold:** bail after the **first** no-progress gap round (aggressive — chosen for max
time/budget savings; a transient slow round ending the loop one round early is acceptable).

**Give-up action:** **just finalize** (`goto synthesize_narrative`). Stuck properties stay
`missing` and the narrative renders them not-found, same as any uncovered property. No status is
written — deliberately NOT `confirmed_absent`, which would assert a false "no data exists" for a
property whose data merely failed to extract. No LLM call.

### State

One new field on `AgentState`: `prev_incomplete_props: list[str]`, replace-on-update (no reducer —
same as the existing `target_properties`). Defaults to absent/None; only set when routing to a gap
round.

### Walkthrough

- First assessment (`rounds_used == 0`): `no_progress` is False (guarded by `rounds_used >= 1`).
  If incomplete + budget → gap round, store `prev_incomplete_props = incomplete_0`.
- Gap round 1 → reassess (`rounds_used == 1`): if `incomplete_1 == incomplete_0` (closed nothing) →
  bail → finalize. If it shrank → continue, store `incomplete_1`, maybe gap round 2.
- Any later gap round that closes nothing → bail. Budget-exhausted path unchanged.

## Testing

Deterministic, mocking the DB/profile/completeness the way the existing `assess_completeness` tests
do (`tests/test_gaploop_bailout.py`):

- **No-progress gap round** — `rounds_used == 1`, `prev_incomplete_props == incomplete` →
  `goto == "synthesize_narrative"`.
- **Progress gap round** — `incomplete` is a strict subset of `prev_incomplete_props` →
  `goto == "write_research_brief"`, and the returned update carries `prev_incomplete_props ==
  incomplete` and `fact_rounds_used == rounds_used + 1`.
- **First assessment never bails** — `rounds_used == 0`, incomplete non-empty, no
  `prev_incomplete_props` → `goto == "write_research_brief"` (gap round), not finalize.
- **Budget exhausted** — `rounds_used + 1 == max_profile_rounds`, incomplete non-empty →
  `goto == "synthesize_narrative"` (unchanged behavior).
- **Brief-scoping verification (no new code)** — with a persisted dossier for the subject,
  `write_research_brief` produces a gap-scoped brief containing the `missing_information`, not the
  whole-profile "comprehensive" brief.

## Files touched

- `src/open_deep_research/deep_researcher.py` — `assess_completeness` bail-out decision.
- `src/open_deep_research/state.py` — new `prev_incomplete_props` field on `AgentState`.
- `tests/test_gaploop_bailout.py` — new.

## Non-goals

- Not fixing `data_protection_law` extraction (follow-up #3 — separate).
- Not adding a `searched_unresolved` status or cross-run "gave-up" memory (explicitly deferred —
  minimal finalize chosen).
- Not changing `max_profile_rounds`, the absence-judge pass, or topic scoping (already handled).

## Risks / open questions

- **Aggressive threshold corner case:** a gap round that *would* have closed a gap on a retry is cut
  off one round early. Accepted by choice. If this proves too eager in practice, the threshold
  becomes a one-line change (track a `no_progress_streak` and bail at 2).
- **`prev_incomplete_props` ordering:** compared as a `set`, so list order from `target_properties`
  is irrelevant. No reducer, so it replaces cleanly each round (matches `target_properties`).
