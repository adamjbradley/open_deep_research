# Gap-loop No-progress Bail-out Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the whole-profile gap loop as soon as a gap round closes zero gaps, instead of burning every remaining `max_profile_rounds`.

**Architecture:** Extract the gap-loop routing decision into a pure, unit-testable helper `_gaploop_decision(...)` that returns `synthesize_narrative` (finalize) when a gap round made no progress (the still-incomplete required-property set didn't shrink). Thread the previous round's incomplete set through a new `AgentState.prev_incomplete_props` field so each round can compare. Zero extra LLM cost; `persist_research`/finalize unchanged.

**Tech Stack:** Python 3.11, pydantic v2, LangGraph, pytest. Spec: `docs/superpowers/specs/2026-06-20-gaploop-bailout-design.md`.

## Global Constraints

- Tests run with `.venv/bin/python -m pytest` (the worktree's own venv via `uv sync`; bare `python` not on PATH).
- On branch `feat/gaploop-bailout` in a worktree — do NOT branch again or touch `main`.
- **No LLM cost** in the bail-out path (pure list/set comparison only).
- **Signal:** no progress ⇔ `set(incomplete) == set(prev_incomplete)` (the incomplete required set didn't shrink).
- **Threshold:** bail after the FIRST no-progress gap round (`rounds_used >= 1`).
- **Give-up action:** just `goto synthesize_narrative`. Do NOT write any property status (NOT `confirmed_absent`).
- `incomplete` only ever stays-same or shrinks across rounds (a complete property never un-completes), so set-equality is a valid no-progress test.

---

### Task 1: No-progress bail-out (`_gaploop_decision` + state field + wiring)

**Files:**
- Modify: `src/open_deep_research/state.py` (`AgentState`, after `fact_rounds_used`)
- Modify: `src/open_deep_research/deep_researcher.py` (add `_gaploop_decision` near `assess_completeness`; replace the loop/finalize decision inside `assess_completeness`)
- Test: `tests/test_gaploop_bailout.py`

**Interfaces:**
- Produces: `_gaploop_decision(incomplete: list[str], prev_incomplete: Optional[list[str]], rounds_used: int, max_rounds: int) -> tuple[str, bool]` returning `(goto, no_progress)` where `goto ∈ {"write_research_brief", "synthesize_narrative"}`.
- Consumes (in `assess_completeness`): the already-computed `incomplete` list, `state.get("prev_incomplete_props")`, `rounds_used`, `configurable.max_profile_rounds`.

- [ ] **Step 1: Write the failing tests for the pure helper**

```python
# tests/test_gaploop_bailout.py
from open_deep_research.deep_researcher import _gaploop_decision


def test_no_progress_gap_round_bails():
    # a gap round (rounds_used=1) whose incomplete set is unchanged -> finalize
    goto, no_progress = _gaploop_decision(["data_protection_law"], ["data_protection_law"], 1, 6)
    assert goto == "synthesize_narrative"
    assert no_progress is True


def test_progress_gap_round_continues():
    # incomplete shrank (a gap closed) -> another gap round
    goto, no_progress = _gaploop_decision(["data_protection_law"], ["data_protection_law", "biometric_capture"], 1, 6)
    assert goto == "write_research_brief"
    assert no_progress is False


def test_first_assessment_never_bails():
    # rounds_used=0, no prev -> gap round even though incomplete (can't be "no progress" yet)
    goto, no_progress = _gaploop_decision(["x", "y"], None, 0, 6)
    assert goto == "write_research_brief"
    assert no_progress is False


def test_budget_exhausted_finalizes():
    # rounds_used+1 == max_rounds -> finalize via budget (not the bail flag)
    goto, no_progress = _gaploop_decision(["x"], ["x", "y"], 5, 6)
    assert goto == "synthesize_narrative"
    assert no_progress is False


def test_all_complete_finalizes():
    # nothing incomplete -> finalize regardless
    goto, _ = _gaploop_decision([], ["x"], 1, 6)
    assert goto == "synthesize_narrative"
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `.venv/bin/python -m pytest tests/test_gaploop_bailout.py -v`
Expected: FAIL — `_gaploop_decision` not defined (ImportError).

- [ ] **Step 3: Add the pure helper**

Add to `deep_researcher.py` immediately above `async def assess_completeness`:
```python
def _gaploop_decision(incomplete, prev_incomplete, rounds_used, max_rounds):
    """Pure whole-profile gap-loop routing decision (no I/O).

    Returns ``(goto, no_progress)``:
      - ``goto``: "write_research_brief" for another gap round, else "synthesize_narrative" (finalize).
      - ``no_progress``: True when a gap round (rounds_used >= 1) closed ZERO gaps -- the
        still-incomplete required-property set is unchanged from the prior round. ``incomplete``
        only stays-same or shrinks across rounds, so set-equality is a valid no-progress test.

    Bail-out: the first no-progress gap round finalizes instead of looping (aggressive threshold).
    """
    no_progress = (
        rounds_used >= 1
        and prev_incomplete is not None
        and set(incomplete) == set(prev_incomplete)
    )
    if incomplete and not no_progress and rounds_used + 1 < max_rounds:
        return "write_research_brief", no_progress
    return "synthesize_narrative", no_progress
```

- [ ] **Step 4: Run helper tests, verify pass**

Run: `.venv/bin/python -m pytest tests/test_gaploop_bailout.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Add the state field**

In `src/open_deep_research/state.py`, inside `class AgentState(MessagesState)`, add after the
`fact_rounds_used: Optional[int]` line:
```python
    # Whole-profile gap loop: the still-incomplete required-property set from the PREVIOUS round,
    # used to detect a no-progress gap round (no reducer -> replaced each round, like target_properties).
    prev_incomplete_props: Optional[list[str]]
```

- [ ] **Step 6: Wire the helper into `assess_completeness`**

In `assess_completeness` (deep_researcher.py), find the decision block that currently reads:
```python
    if incomplete and rounds_used + 1 < configurable.max_profile_rounds:
        logger.info("Whole-profile incomplete (%s); gap round %d", incomplete, rounds_used + 1)
        gap = (
            "These profile properties are still incomplete and MUST be resolved or, if no data "
            "exists, explicitly confirmed unavailable after searching: "
            + ", ".join(f"{p} ({ledger.get(p)})" for p in incomplete) + "."
        )
        return Command(
            goto="write_research_brief",
            update={"missing_information": gap, "target_properties": incomplete,
                    "fact_rounds_used": rounds_used + 1},
        )
    if incomplete:
        logger.info("Whole-profile still incomplete %s but round budget exhausted; finishing", incomplete)
    return Command(goto="synthesize_narrative", update={"fact_rounds_used": rounds_used})
```
Replace that entire block with:
```python
    goto, no_progress = _gaploop_decision(
        incomplete, state.get("prev_incomplete_props"), rounds_used, configurable.max_profile_rounds
    )
    if goto == "write_research_brief":
        logger.info("Whole-profile incomplete (%s); gap round %d", incomplete, rounds_used + 1)
        gap = (
            "These profile properties are still incomplete and MUST be resolved or, if no data "
            "exists, explicitly confirmed unavailable after searching: "
            + ", ".join(f"{p} ({ledger.get(p)})" for p in incomplete) + "."
        )
        return Command(
            goto="write_research_brief",
            update={"missing_information": gap, "target_properties": incomplete,
                    "fact_rounds_used": rounds_used + 1,
                    "prev_incomplete_props": incomplete},
        )
    if no_progress:
        logger.info("Gap round closed zero gaps (%s unchanged); bailing out to finalize", incomplete)
    elif incomplete:
        logger.info("Whole-profile still incomplete %s but round budget exhausted; finishing", incomplete)
    return Command(goto="synthesize_narrative", update={"fact_rounds_used": rounds_used})
```

- [ ] **Step 7: Verify helper tests still pass + graph compiles**

Run: `.venv/bin/python -m pytest tests/test_gaploop_bailout.py -v`
Expected: PASS (5).
Run: `.venv/bin/python -c "import open_deep_research.deep_researcher as d; d._gaploop_decision; d.assess_completeness; print('ok')"`
Expected: `ok`

- [ ] **Step 8: Commit**

```bash
git add src/open_deep_research/state.py src/open_deep_research/deep_researcher.py tests/test_gaploop_bailout.py
git commit -m "feat(dossier): no-progress bail-out for the whole-profile gap loop"
```

---

### Task 2: Brief-scoping verification test (no new code)

**Files:**
- Test: `tests/test_gaploop_bailout.py` (add)

**Interfaces:**
- Consumes: `dr.write_research_brief`, `dr.get_subject_by_slug` (monkeypatched to return a dossier).

Guards the spec's assumption that — because partial-persist (#45) now persists a dossier each round —
`write_research_brief` takes its **gap-scoped** branch (focus on missing info + the dossier) rather
than the whole-profile "comprehensive" brief. With `subject` + `target_properties` set and the
dossier read mocked, this path is LLM-free and DB-free.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gaploop_bailout.py (add)
import asyncio
from langchain_core.messages import HumanMessage
from open_deep_research import deep_researcher as dr


def test_gap_round_brief_is_scoped_when_dossier_exists(monkeypatch):
    async def fake_get_subject(db_path, slug):
        return {"name": "Estonia",
                "current_report": "## Prior dossier\n- foundational_id_scheme: ID card",
                "sources": []}
    monkeypatch.setattr(dr, "get_subject_by_slug", fake_get_subject)

    state = {
        "messages": [HumanMessage(content="Research Estonia's digital identity")],
        "subject": "Estonia",
        "missing_information": "data_protection_law (missing_value)",
        "target_properties": ["data_protection_law"],
    }
    cfg = {"configurable": {"whole_profile_mode": True, "database_path": "/tmp/gaploop_brief.db",
                            "profile_name": "country_digital_identity", "thread_id": "t"}}
    result = asyncio.run(dr.write_research_brief(state, cfg))
    brief = result["research_brief"]
    # gap-scoped branch fired: it focuses on the missing info + cites the prior dossier
    assert "currently missing" in brief.lower()
    assert "data_protection_law" in brief
    assert "Prior dossier" in brief
```

- [ ] **Step 2: Run it, verify it passes (the behavior already exists)**

Run: `.venv/bin/python -m pytest tests/test_gaploop_bailout.py::test_gap_round_brief_is_scoped_when_dossier_exists -v`
Expected: PASS. (If it FAILS because `write_research_brief` makes an unmocked model/DB call on this
path, that is a real finding — report it: the gap-scoped branch was assumed LLM-free. Do NOT add
broad mocks to force it green; surface the gap.)

- [ ] **Step 3: No-regression — run the affected suites + compile**

Run: `.venv/bin/python -m pytest tests/test_gaploop_bailout.py tests/test_facts_first_mode.py tests/test_partial_persist.py -p no:warnings -q -o addopts=""`
Expected: PASS (Task 1 helper tests + this test + facts-first routing + partial-persist all green).
Run: `.venv/bin/python -c "import open_deep_research.deep_researcher as d; d.assess_completeness; d.write_research_brief; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add tests/test_gaploop_bailout.py
git commit -m "test(dossier): verify gap-round brief is scoped when a dossier exists"
```

---

## Self-Review

**Spec coverage:** signal (`set(incomplete)==set(prev_incomplete)`) → Task 1 helper; mechanic +
threshold (bail after 1) + give-up action (finalize, no status write) → Task 1 helper + wiring;
state field `prev_incomplete_props` → Task 1 Step 5; the four behavior cases (no-progress, progress,
first-assessment, budget) → Task 1 tests; brief-scoping verification → Task 2. Non-goals (no status
write, no `max_profile_rounds` change, no extraction fix) respected — Task 1 writes no status. All
spec sections mapped.

**Placeholder scan:** none. Task 2 Step 2's "if it fails, report it" is a concrete instruction to
surface a real finding, not a placeholder.

**Type consistency:** `_gaploop_decision(incomplete, prev_incomplete, rounds_used, max_rounds) ->
(goto, no_progress)` used identically in the tests (Task 1 Step 1) and the wiring (Task 1 Step 6);
`prev_incomplete_props` field name consistent across state.py, the wiring update, and `state.get(...)`;
`goto` string literals match the `Command(goto=...)` targets.

**Ordering:** Task 1 (helper + state + wiring) → Task 2 (verification test + no-regression).
