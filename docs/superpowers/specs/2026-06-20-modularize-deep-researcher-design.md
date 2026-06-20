# Modularize `deep_researcher.py` — Design

**Date:** 2026-06-20
**Status:** Approved (design); pending implementation plan
**Origin:** `deep_researcher.py` has grown to ~2,415 lines (~50 top-level functions/classes spanning
the whole pipeline). Goal: cap its size and expose each phase as a reusable component, so alternate
**lighter research approaches** (recomposing the researcher + extraction primitives behind a
different graph assembly) can be built and compared without duplicating this code.

## Goals

1. Cap `deep_researcher.py` size (target ~250 lines: routes + graph assembly + re-exports only).
2. Every node + helper becomes an importable, independently reusable component.
3. **Zero behavior change** — functions move verbatim; the compiled `deep_researcher` graph is
   identical (same nodes, edges, behavior). This is a pure refactor.

Reuse boundary (confirmed): nearly everything is a reusable primitive; only the top-level graph
**assembly** varies between approaches. A new approach = a new assembler importing the same `nodes/`
modules.

## Section 1 — Module layout

New package `open_deep_research/nodes/`, one module per phase:

| Module | Functions/classes |
|---|---|
| `nodes/common.py` | `_report_is_failed`, `_is_empty_run`, `_run_fact_count`, `_raw_text_source_count`, `_fact_fetch_text`, `recommended_recursion_limit` |
| `nodes/profiles.py` | `select_profile`, `resolve_target_properties`, `_effective_profile_name`, `_resolve_subject` |
| `nodes/brief.py` | `clarify_with_user`, `assess_knowledge`, `answer_from_dossier`, `write_research_brief`, `_steer_brief_with_catalog` |
| `nodes/supervisor.py` | `supervisor`, `supervisor_tools`, `_lead_researcher_tools` |
| `nodes/researcher.py` | `researcher`, `researcher_tools`, `compress_research`, `execute_tool_safely`, the compiled researcher subgraph |
| `nodes/extraction.py` | `FactRecord`, `ExtractionResult`, `_make_fact_model_call`, `_maybe_propose_extensions`, `preallocate_run`, `extract_facts` |
| `nodes/completeness.py` | `assess_sufficiency`, `assess_completeness`, `_gaploop_decision`, `_target_property_coverage`, `AbsenceJudgement`, `judge_absence`, `_make_absence_judge_call` |
| `nodes/synthesis.py` | `synthesize_narrative`, `answer_from_facts`, `_facts_answer_text`, `_synthesize_dossier`, `_best_singular_row`, `_display_value`, `NameConsolidation`, `_consolidate_name_group`, `_make_name_consolidation_call` |
| `nodes/persistence.py` | `persist_research`, `_checkpoint_dossier`, `_facts_report_md`, `_merge_dossier` |
| `nodes/report.py` | `final_report_generation` |

`deep_researcher.py` retains only: `route_after_research`/`route_after_extract`, the graph builders
(supervisor / researcher subgraph reference / deep_researcher), `compile()` + the `deep_researcher`
export, and the re-export imports.

Note: `nodes/completeness.py` is distinct from the existing `factbase/completeness.py` (different
package; the `nodes.` prefix disambiguates).

## Section 2 — Re-export & the monkeypatch subtlety

**Import back-compat:** the assembler imports every node it wires
(`from .nodes.completeness import assess_completeness, _gaploop_decision`, …). Those imports *are*
the re-exports, so `langgraph.json`'s entry point and every `from open_deep_research import
deep_researcher as dr; dr.X` keeps resolving. A missing re-export surfaces immediately as
`AttributeError` when the full suite runs.

**Monkeypatch targets must move.** Tests that patch a helper through the `dr` namespace
(`monkeypatch.setattr(dr, "save_run_and_upsert_subject", …)`, `dr.get_subject_by_slug`,
`dr._facts_report_md`, `dr.get_all_tools`, `dr.execute_tool_safely`, …) rely on the name being
resolved in `deep_researcher`'s namespace. Once the function that *uses* the helper moves to a
`nodes/` module, the patch must target that module
(`monkeypatch.setattr(persistence, "save_run_and_upsert_subject", …)`). Affected: the partial-persist
tests, the researcher premature-guard test, the gaploop brief-scoping test. Each is caught the moment
its module is extracted (the suite goes red until the patch target is updated).

**Behavior invariant:** functions move byte-for-byte; only their home module and the test patch
targets change. Tests that import-and-call `dr.X` without patching are unaffected.

## Section 3 — Import structure (no cycles)

Graph transitions are strings (`Command(goto="researcher")`), so the only import edges are direct
calls. Shared helpers sit at the bottom, yielding an acyclic DAG (arrows = "imports from"):

```
common, profiles, extraction, synthesis, researcher, report   (leaves / low)
persistence  → profiles, common
completeness → persistence, profiles, common
supervisor   → researcher            (supervisor_tools invokes the researcher subgraph)
brief        → profiles, persistence
deep_researcher.py (assembler) → ALL
```

Two couplings handled explicitly:
- `_resolve_subject` + `_effective_profile_name` are shared (brief, persistence, completeness) → they
  live in `profiles.py` (lowest common point), so `brief`/`persistence`/`completeness` never import
  each other.
- The **researcher subgraph** (invoked by `supervisor_tools` via `ConductResearch`) is built+compiled
  in `nodes/researcher.py` and imported by `supervisor.py` + the assembler — a one-way edge.

Anything that turns out genuinely mutual gets pushed down into `common.py`.

## Section 4 — Incremental extraction order + behavior proof

Bottom-up (least-coupled first); each module = one task = one commit:

1. `common.py` → 2. `profiles.py` → 3. `report.py` → 4. `extraction.py` → 5. `synthesis.py` →
6. `persistence.py` → 7. `completeness.py` → 8. `researcher.py` (+ subgraph) → 9. `supervisor.py` →
10. `brief.py` → 11. final sweep (`deep_researcher.py` = routes + builders + compile + re-exports,
confirm ~250 lines, no business logic).

**Each task, identical shape:** move the functions *verbatim* into the new module; add the
`from .nodes.<mod> import …` line to the assembler (wires the graph *and* re-exports); update any
monkeypatch test that targeted a moved function; run the full suite; commit.

**Behavior-unchanged proof at every step:**
- **Full suite green** — `.venv/bin/python -m pytest -p no:warnings -q` (the regression net; the
  reason for one-module-at-a-time).
- **Graph-identity assertion** — capture `set(deep_researcher.get_graph().nodes)` (and edge set)
  before task 1; assert it's unchanged after each task (a small `tests/test_graph_identity.py`).
- **Pure-move review** — each task's diff is deletion of N lines from `deep_researcher.py` + addition
  of the same N lines in the new module (plus import/re-export lines); a reviewer confirms no logic
  changed.

## Files

- New: `open_deep_research/nodes/__init__.py` + the 10 modules above.
- New: `tests/test_graph_identity.py` (node/edge snapshot guard).
- Modified: `deep_researcher.py` (shrinks to the assembler); monkeypatch test files
  (partial-persist, premature-guard, gaploop) get their patch targets updated.

## Non-goals

- No logic/behavior change — not a place to fix bugs or refactor function internals.
- No change to `state.py`, `configuration.py`, `utils.py`, `storage.py`, `factbase/` (already
  modular; nodes import from them unchanged).
- Not building an alternate lighter approach yet — this only makes the components reusable. The
  alternate approach is a follow-up (a new assembler).
- Not renaming the `deep_researcher` export or moving the entry point (`langgraph.json` unchanged).

## Risks / open questions

- **The researcher subgraph relocation** (task 8) is the one non-trivial move — `supervisor_tools`
  must still reach the compiled subgraph. Verify the import direction (`supervisor → researcher`)
  compiles and the graph-identity assertion holds.
- **Hidden monkeypatch targets:** a test may patch `dr.X` for an `X` we don't anticipate. The full
  suite after each task surfaces these; the fix is mechanical (repoint the patch).
- **`nodes/` package name** is provisional; rename is a find/replace if preferred (`agent/`,
  `stages/`).
