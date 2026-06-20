# Modularize deep_researcher.py Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the ~50 functions in the 2,439-line `deep_researcher.py` into an `open_deep_research/nodes/` package (one module per phase), leaving `deep_researcher.py` as a ~250-line assembler — with zero behavior change.

**Architecture:** Pure refactor by verbatim function moves, bottom-up (least-coupled module first). `deep_researcher.py` re-exports every moved symbol (so `dr.X` imports + `langgraph.json` keep working) and retains only the routes + graph builders + `compile()`. Each move is guarded by the full test suite + a graph-identity snapshot (node/edge set unchanged).

**Tech Stack:** Python 3.11, LangGraph, pytest. Spec: `docs/superpowers/specs/2026-06-20-modularize-deep-researcher-design.md`.

## Global Constraints

- Tests run with `.venv/bin/python -m pytest` (the worktree's own venv via `uv sync`; bare `python` not on PATH).
- On branch `feat/modularize-deep-researcher` in a worktree — do NOT branch again or touch `main`.
- **ZERO behavior change.** Functions move byte-for-byte; do not edit any function body, signature, or logic. The only edits are: which file a function lives in, the new module's import lines, the assembler's re-export lines, and monkeypatch *targets* in tests.
- New package dir: `src/open_deep_research/nodes/` with `__init__.py` (empty is fine).
- `nodes/completeness.py` is distinct from the existing `factbase/completeness.py` (different package).

### Per-extraction recipe (every Task 3–12 follows this; specifics are per task)

1. **Create `src/open_deep_research/nodes/<mod>.py`** with: a one-line module docstring; `import logging` + `logger = logging.getLogger(__name__)`; and the import lines the moved functions reference. Source the imports from `deep_researcher.py`'s top block (lines 1–82): `from datetime import ...`, `from typing import Optional, Literal`, `from langchain_core.messages import ...`, `from langchain_core.runnables import RunnableConfig`, `from langgraph.types import Command`, `from pydantic import BaseModel, Field`, and the `open_deep_research.{configuration,state,storage,utils,prompts,claude_agent_chat,failover}` imports — copy only the names the moved functions actually use.
2. **Cut the listed functions verbatim** out of `deep_researcher.py` and paste them into the new module (preserve order and bodies exactly).
3. **In `deep_researcher.py`, add** `from open_deep_research.nodes.<mod> import <names>` (near the other node imports). This wires the graph builder (which references these names) AND re-exports them so `dr.<name>` still resolves.
4. **Update monkeypatch tests** named in the task (repoint `monkeypatch.setattr(dr, "X", …)` → `monkeypatch.setattr(<mod>, "X", …)`, adding `from open_deep_research.nodes import <mod>` to the test). If a task lists no tests, none need changing.
5. **Verify + commit:** run the full suite (`.venv/bin/python -m pytest -p no:warnings -q`) → all green; the graph-identity test (Task 1) must pass. Commit `git add -A && git commit -m "refactor(nodes): extract <mod> from deep_researcher"`.

If the suite goes red with `ImportError`/`NameError`, a needed import was missed in step 1 — add it. If `AttributeError: module 'deep_researcher' has no attribute X`, a re-export was missed in step 3 — add it.

---

### Task 1: Graph-identity snapshot test (the safety net)

**Files:**
- Create: `tests/test_graph_identity.py`

**Interfaces:**
- Produces: a regression guard asserting `deep_researcher`'s compiled node set is a fixed snapshot — every later task must keep it green.

- [ ] **Step 1: Capture the current node set**

Run: `.venv/bin/python -c "from open_deep_research.deep_researcher import deep_researcher as g; print(sorted(g.get_graph().nodes))"`
Record the printed list (the canonical node ids).

- [ ] **Step 2: Write the test using that exact list**

```python
# tests/test_graph_identity.py
"""Guards the modularization refactor: the compiled deep_researcher graph's node set must not
change as functions move into the nodes/ package. Update EXPECTED_NODES only for an intentional
graph change (not a move)."""
from open_deep_research.deep_researcher import deep_researcher

# Paste the exact sorted list printed in Step 1 between the brackets:
EXPECTED_NODES = {
    # <-- fill from Step 1 output, e.g. "__start__", "clarify_with_user", "write_research_brief", ...
}


def test_graph_node_set_is_stable():
    nodes = set(deep_researcher.get_graph().nodes)
    assert nodes == EXPECTED_NODES, f"node set drift: +{nodes - EXPECTED_NODES} -{EXPECTED_NODES - nodes}"


def test_graph_compiles_and_has_entry():
    g = deep_researcher.get_graph()
    assert "__start__" in {str(n) for n in g.nodes}
```

- [ ] **Step 3: Run it, verify pass**

Run: `.venv/bin/python -m pytest tests/test_graph_identity.py -v -o addopts=""`
Expected: PASS (2). If `test_graph_node_set_is_stable` fails, `EXPECTED_NODES` doesn't match Step 1 — fix the literal.

- [ ] **Step 4: Commit**

```bash
git add tests/test_graph_identity.py
git commit -m "test: graph-identity snapshot guard for the modularization refactor"
```

---

### Task 2: Extract `nodes/common.py`

**Files:**
- Create: `src/open_deep_research/nodes/__init__.py` (empty), `src/open_deep_research/nodes/common.py`
- Modify: `src/open_deep_research/deep_researcher.py`

**Move (verbatim):** `_report_is_failed`, `_is_empty_run`, `_run_fact_count`, `_raw_text_source_count`, `_fact_fetch_text`, `recommended_recursion_limit`.
**Re-export line:** `from open_deep_research.nodes.common import (_report_is_failed, _is_empty_run, _run_fact_count, _raw_text_source_count, _fact_fetch_text, recommended_recursion_limit)`
**Monkeypatch tests to update:** none.

- [ ] **Step 1:** Create `nodes/__init__.py` (empty) and follow the per-extraction recipe (Global Constraints) for `common.py` with the functions above. `_fact_fetch_text` uses `factbase.fetch`; the count helpers use `aiosqlite`/`storage`; `recommended_recursion_limit` is pure.
- [ ] **Step 2:** Run `.venv/bin/python -m pytest -p no:warnings -q` → all green (incl. `tests/test_graph_identity.py`).
- [ ] **Step 3:** Commit `refactor(nodes): extract common from deep_researcher`.

---

### Task 3: Extract `nodes/profiles.py`

**Move (verbatim):** `select_profile`, `resolve_target_properties`, `_effective_profile_name`, `_resolve_subject`.
**Re-export line:** `from open_deep_research.nodes.profiles import (select_profile, resolve_target_properties, _effective_profile_name, _resolve_subject)`
**Monkeypatch tests to update:** none.

- [ ] **Step 1:** Follow the recipe for `profiles.py`. These use `factbase.profile`, `configuration`, the LLM call helpers; `_resolve_subject` uses `storage.get_subject_names`. Import what they reference.
- [ ] **Step 2:** Full suite green.
- [ ] **Step 3:** Commit `refactor(nodes): extract profiles from deep_researcher`.

---

### Task 4: Extract `nodes/report.py`

**Move (verbatim):** `final_report_generation`.
**Re-export line:** `from open_deep_research.nodes.report import final_report_generation`
**Monkeypatch tests to update:** none.

- [ ] **Step 1:** Follow the recipe for `report.py`.
- [ ] **Step 2:** Full suite green.
- [ ] **Step 3:** Commit `refactor(nodes): extract report from deep_researcher`.

---

### Task 5: Extract `nodes/extraction.py`

**Move (verbatim):** `FactRecord`, `ExtractionResult`, `_make_fact_model_call`, `_maybe_propose_extensions`, `preallocate_run`, `extract_facts`.
**Re-export line:** `from open_deep_research.nodes.extraction import (FactRecord, ExtractionResult, _make_fact_model_call, _maybe_propose_extensions, preallocate_run, extract_facts)`
**Monkeypatch tests to update:** none expected (`tests/test_lean_extract*.py` import from `factbase.lean_extract`, not `dr`). If the suite flags a `dr.`-patched extraction symbol, repoint it to `extraction`.

- [ ] **Step 1:** Follow the recipe for `extraction.py`. Uses `factbase` (lean_extract, schema, ingest), `storage`, `nodes.common._fact_fetch_text` (import from `nodes.common`).
- [ ] **Step 2:** Full suite green.
- [ ] **Step 3:** Commit `refactor(nodes): extract extraction from deep_researcher`.

---

### Task 6: Extract `nodes/synthesis.py`

**Move (verbatim):** `synthesize_narrative`, `answer_from_facts`, `_facts_answer_text`, `_synthesize_dossier`, `_best_singular_row`, `_display_value`, `NameConsolidation`, `_consolidate_name_group`, `_make_name_consolidation_call`.
**Re-export line:** `from open_deep_research.nodes.synthesis import (synthesize_narrative, answer_from_facts, _facts_answer_text, _synthesize_dossier, _best_singular_row, _display_value, NameConsolidation, _consolidate_name_group, _make_name_consolidation_call)`
**Monkeypatch tests to update:** none expected. If the suite flags a `dr.`-patched synthesis symbol, repoint it to `synthesis`.

- [ ] **Step 1:** Follow the recipe for `synthesis.py`. Uses `factbase.query`/`render`, `configuration`.
- [ ] **Step 2:** Full suite green.
- [ ] **Step 3:** Commit `refactor(nodes): extract synthesis from deep_researcher`.

---

### Task 7: Extract `nodes/persistence.py`

**Move (verbatim):** `persist_research`, `_checkpoint_dossier`, `_facts_report_md`, `_merge_dossier`.
**Re-export line:** `from open_deep_research.nodes.persistence import (persist_research, _checkpoint_dossier, _facts_report_md, _merge_dossier)`
**Monkeypatch tests to update:** `tests/test_partial_persist.py` — the checkpoint-helper tests (`test_checkpoint_persists_partial_when_facts_and_new_subject`, `test_checkpoint_skips_when_no_facts`, `test_checkpoint_skips_existing_dossier`, `test_checkpoint_skips_when_no_subject`) patch `dr._run_fact_count`, `dr.get_subject_by_slug`, `dr._facts_report_md`, `dr.save_run_and_upsert_subject`. These are all resolved inside `_checkpoint_dossier`, which now lives in `persistence` — repoint every one to `persistence` (add `from open_deep_research.nodes import persistence`). Leave `test_assess_completeness_invokes_checkpoint` (which patches `dr._checkpoint_dossier`) for Task 8.

- [ ] **Step 1:** Follow the recipe for `persistence.py`. Imports: `storage` (`save_run_and_upsert_subject`, `get_subject_by_slug`, `log_research_run`, `get_subject_names`, …), `nodes.common` (`_run_fact_count`, `_raw_text_source_count`, `_is_empty_run`, `_report_is_failed`), `nodes.profiles` (`_resolve_subject`), `factbase`, `utils.extract_sources`.
- [ ] **Step 2:** Repoint the four `test_partial_persist.py` checkpoint-helper tests' patches to `persistence` (per above).
- [ ] **Step 3:** Full suite green (incl. `test_partial_persist.py`).
- [ ] **Step 4:** Commit `refactor(nodes): extract persistence from deep_researcher`.

---

### Task 8: Extract `nodes/completeness.py`

**Move (verbatim):** `assess_sufficiency`, `assess_completeness`, `_gaploop_decision`, `_target_property_coverage`, `AbsenceJudgement`, `judge_absence`, `_make_absence_judge_call`.
**Re-export line:** `from open_deep_research.nodes.completeness import (assess_sufficiency, assess_completeness, _gaploop_decision, _target_property_coverage, AbsenceJudgement, judge_absence, _make_absence_judge_call)`
**Monkeypatch tests to update:** `tests/test_partial_persist.py::test_assess_completeness_invokes_checkpoint` patches `dr._checkpoint_dossier` (the spy). `assess_completeness` now lives in `completeness` and calls `_checkpoint_dossier` imported into `completeness` — repoint the spy to `completeness` (`monkeypatch.setattr(completeness, "_checkpoint_dossier", spy)`; add `from open_deep_research.nodes import completeness`). The `test_gaploop_bailout.py` `_gaploop_decision` tests import-and-call (no patch) — unaffected.

- [ ] **Step 1:** Follow the recipe for `completeness.py`. Imports: `factbase` (completeness, entities, query, profile, schema, migrations, property_status), `nodes.persistence._checkpoint_dossier`, `nodes.profiles._effective_profile_name`, `aiosqlite`.
- [ ] **Step 2:** Repoint `test_assess_completeness_invokes_checkpoint`'s patch to `completeness`.
- [ ] **Step 3:** Full suite green (incl. `test_gaploop_bailout.py`, `test_partial_persist.py`).
- [ ] **Step 4:** Commit `refactor(nodes): extract completeness from deep_researcher`.

---

### Task 9: Extract `nodes/researcher.py`

**Move (verbatim):** `researcher`, `researcher_tools`, `compress_research`, `execute_tool_safely`, AND the researcher subgraph build (`researcher_builder = StateGraph(...)` … `.compile()` — the `researcher_builder`/compiled-subgraph block currently in `deep_researcher.py`). Export the compiled subgraph under its existing name.
**Re-export line:** `from open_deep_research.nodes.researcher import (researcher, researcher_tools, compress_research, execute_tool_safely)` plus the subgraph name the assembler/supervisor reference.
**Monkeypatch tests to update:** `tests/test_researcher_premature_guard.py` patches `dr.get_all_tools` and `dr.execute_tool_safely` (both resolved inside `researcher_tools`) — repoint to `researcher` (add `from open_deep_research.nodes import researcher`).

- [ ] **Step 1:** Identify the researcher subgraph block (`grep -n "researcher_builder" src/open_deep_research/deep_researcher.py`). Follow the recipe for `researcher.py`, moving the four functions + the subgraph builder/compile. Imports: `utils` (`get_all_tools`, `get_notes_from_tool_calls`, …), `configuration`, `state` (`ResearcherState`, `ResearcherOutputState`), `langgraph`.
- [ ] **Step 2:** Repoint `test_researcher_premature_guard.py` patches to `researcher`.
- [ ] **Step 3:** Full suite green (incl. `test_researcher_premature_guard.py`).
- [ ] **Step 4:** Commit `refactor(nodes): extract researcher (+ subgraph) from deep_researcher`.

---

### Task 10: Extract `nodes/supervisor.py`

**Move (verbatim):** `supervisor`, `supervisor_tools`, `_lead_researcher_tools`, AND the supervisor subgraph build (`supervisor_builder = StateGraph(...)` … `.compile()`).
**Re-export line:** `from open_deep_research.nodes.supervisor import (supervisor, supervisor_tools, _lead_researcher_tools)` plus the supervisor subgraph name the assembler references.
**Monkeypatch tests to update:** none expected. If the suite flags a `dr.`-patched supervisor symbol, repoint it to `supervisor`.

- [ ] **Step 1:** Follow the recipe for `supervisor.py`. It imports the researcher subgraph from `nodes.researcher` (the `supervisor → researcher` edge). Imports: `state` (`SupervisorState`), `utils`, `configuration`, `langgraph`.
- [ ] **Step 2:** Full suite green.
- [ ] **Step 3:** Commit `refactor(nodes): extract supervisor (+ subgraph) from deep_researcher`.

---

### Task 11: Extract `nodes/brief.py`

**Move (verbatim):** `clarify_with_user`, `assess_knowledge`, `answer_from_dossier`, `write_research_brief`, `_steer_brief_with_catalog`.
**Re-export line:** `from open_deep_research.nodes.brief import (clarify_with_user, assess_knowledge, answer_from_dossier, write_research_brief, _steer_brief_with_catalog)`
**Monkeypatch tests to update:** `tests/test_gaploop_bailout.py::test_gap_round_brief_is_scoped_when_dossier_exists` patches `dr.get_subject_by_slug` (resolved inside `write_research_brief`) — repoint to `brief` (add `from open_deep_research.nodes import brief`).

- [ ] **Step 1:** Follow the recipe for `brief.py`. Imports: `storage` (`get_subject_by_slug`), `nodes.profiles` (`_resolve_subject`, `select_profile`, `resolve_target_properties`, `_effective_profile_name`), `factbase.profile`, `prompts`, `configuration`, `state`.
- [ ] **Step 2:** Repoint the brief-scoping test's patch to `brief`.
- [ ] **Step 3:** Full suite green (incl. `test_gaploop_bailout.py`).
- [ ] **Step 4:** Commit `refactor(nodes): extract brief from deep_researcher`.

---

### Task 12: Final sweep + verification

**Files:**
- Modify: `src/open_deep_research/deep_researcher.py`

- [ ] **Step 1: Confirm what remains** — `deep_researcher.py` should now contain only: the top imports + the `from open_deep_research.nodes.* import ...` re-exports, `route_after_research`, `route_after_extract`, the `deep_researcher_builder` (top-level `add_node`/`add_edge`/`add_conditional_edges`), `deep_researcher = deep_researcher_builder.compile(...)`, and the `logger`. Remove any now-unused imports flagged by: `.venv/bin/python -m pyflakes src/open_deep_research/deep_researcher.py` (or `ruff check src/open_deep_research/deep_researcher.py`).

- [ ] **Step 2: Size check** — `wc -l src/open_deep_research/deep_researcher.py` → expect roughly ≤ 300 lines (target ~250). If materially higher, business logic is still present — move it to the right `nodes/` module.

- [ ] **Step 3: Full verification**

Run: `.venv/bin/python -m pytest -p no:warnings -q`
Expected: entire suite green.
Run: `.venv/bin/python -c "from open_deep_research.deep_researcher import deep_researcher as g; print('nodes', len(g.get_graph().nodes)); print('import-ok')"`
Expected: prints the node count + `import-ok` (langgraph entry point intact).
Run: `.venv/bin/python -c "import open_deep_research.deep_researcher as d; [getattr(d, n) for n in ('assess_completeness','_gaploop_decision','_checkpoint_dossier','extract_facts','persist_research','write_research_brief','researcher_tools','execute_tool_safely','recommended_recursion_limit')]; print('re-exports ok')"`
Expected: `re-exports ok` (every commonly-referenced symbol still resolves on `dr`).

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor(nodes): deep_researcher.py is now a thin assembler (~250 lines)"
```

---

## Self-Review

**Spec coverage:** §1 layout → Tasks 2–11 (one module each, exact function lists match the spec table); §2 re-export + monkeypatch migration → the per-extraction recipe (step 3) + the named test repoints in Tasks 7/8/9/11; §3 import structure (`_resolve_subject`/`_effective_profile_name` in profiles; researcher subgraph in researcher; supervisor→researcher) → Tasks 3/9/10; §4 incremental bottom-up order + graph-identity proof → Task 1 (snapshot) + the order of Tasks 2–11 + Task 12 (size + full verification). All spec sections mapped.

**Placeholder scan:** none. Task 1 Step 2's `EXPECTED_NODES` is filled from the Step 1 capture (a concrete procedure, not a placeholder). The "move verbatim" steps reference existing code by exact function name — complete by construction for a refactor (reproducing 2,439 lines in the plan would invite transcription errors; the move + the full-suite guard is the correct instruction).

**Type/name consistency:** the function names in each task's move-list + re-export line match the spec table exactly; module names (`common`, `profiles`, `brief`, `supervisor`, `researcher`, `extraction`, `completeness`, `synthesis`, `persistence`, `report`) are consistent across tasks and the recipe; the monkeypatch repoint targets (`persistence`, `completeness`, `researcher`, `brief`) match where each patched symbol is resolved.

**Ordering:** Task 1 (guard) → 2 common → 3 profiles → 4 report → 5 extraction → 6 synthesis → 7 persistence (→profiles,common) → 8 completeness (→persistence,profiles) → 9 researcher → 10 supervisor (→researcher) → 11 brief (→profiles,persistence) → 12 sweep. Every dependency precedes its dependents.
