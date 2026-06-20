# Partial Dossier Persist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Checkpoint-persist a partial subject dossier mid-loop so a whole-profile run that aborts/times out still saves the dossier it has (subject + facts) instead of nothing.

**Architecture:** Add `_checkpoint_dossier(state, config)` — a cheap, **no-LLM** helper that renders the facts gathered so far (`dossier show`-style markdown) and persists the subject via the existing `save_run_and_upsert_subject` primitive (idempotent on `prealloc_run_id`). Wire it into `assess_completeness`, which runs after every `extract_facts` round. `persist_research` (finalize, full narrative) is unchanged.

**Tech Stack:** Python 3.11, pydantic v2, pytest, the existing `deep_researcher.py` + `factbase` storage/query/render helpers. Spec: `docs/superpowers/specs/2026-06-20-partial-dossier-persist-design.md`.

## Global Constraints

- Tests run with `.venv/bin/python -m pytest` (bare `python` not on PATH).
- On `main`; do NOT branch or touch other presets/files; this is additive to `deep_researcher.py`.
- **No LLM cost in the checkpoint** — it renders facts from the DB and skips if `state["subject"]` isn't set (never triggers `_resolve_subject`'s LLM call).
- **Guard 1:** checkpoint only when `fact_count > 0`.
- **Guard 2:** never overwrite an existing established dossier — checkpoint persists the report only for a subject with no existing `current_report` (brand-new subject).
- Checkpoint run `status = "partial"` (finalize uses `"completed"`).
- Checkpoint is **best-effort**: any error is caught + logged (never fails the run).
- Reuse existing helpers: `_run_fact_count(db_path, run_id)`, `get_subject_by_slug(db_path, slug)`, `save_run_and_upsert_subject(db_path, subject_name, slug, merged_report, sources_union, run, now, run_id)`, `slugify`, `extract_sources`, `factbase.query.FactQuery.show_grouped`, `factbase.render.render`, `factbase.entities.CountryResolver`.

---

### Task 1: `_checkpoint_dossier` helper

**Files:**
- Modify: `src/open_deep_research/deep_researcher.py` (add `_facts_report_md` + `_checkpoint_dossier` near `persist_research`, ~line 1352)
- Test: `tests/test_partial_persist.py`

**Interfaces:**
- Consumes: `_run_fact_count`, `get_subject_by_slug`, `save_run_and_upsert_subject`, `slugify`, `extract_sources` (all already imported in `deep_researcher.py`).
- Produces: `async _facts_report_md(config, instance_key) -> str`; `async _checkpoint_dossier(state, config) -> None`.

- [ ] **Step 1: Write the failing tests** (mock the fact-read + report + spy the persist)

```python
# tests/test_partial_persist.py
import asyncio
from open_deep_research import deep_researcher as dr


def _setup(monkeypatch, *, fact_count, existing):
    calls = {}
    async def fake_fact_count(db_path, run_id): return fact_count
    async def fake_get_subject(db_path, slug): return existing
    async def fake_report(config, ik): return "## Facts\n- foundational_id_scheme: ID card\n"
    async def fake_save(db_path, *, subject_name, slug, merged_report, sources_union, run, now, run_id):
        calls["save"] = {"subject": subject_name, "status": run.get("status"), "report": merged_report, "run_id": run_id}
        return (1, run_id or 7)
    monkeypatch.setattr(dr, "_run_fact_count", fake_fact_count)
    monkeypatch.setattr(dr, "get_subject_by_slug", fake_get_subject)
    monkeypatch.setattr(dr, "_facts_report_md", fake_report)
    monkeypatch.setattr(dr, "save_run_and_upsert_subject", fake_save)
    return calls

_STATE = {"subject": "Estonia", "prealloc_run_id": 7, "research_brief": "b", "raw_notes": []}
_CFG = {"configurable": {"thread_id": "t", "database_path": "/tmp/x.db"}}

def test_checkpoint_persists_partial_when_facts_and_new_subject(monkeypatch):
    calls = _setup(monkeypatch, fact_count=52, existing=None)
    asyncio.run(dr._checkpoint_dossier(_STATE, _CFG))
    assert calls["save"]["subject"] == "Estonia"
    assert calls["save"]["status"] == "partial"
    assert "ID card" in calls["save"]["report"]
    assert calls["save"]["run_id"] == 7      # idempotent on the preallocated run

def test_checkpoint_skips_when_no_facts(monkeypatch):
    calls = _setup(monkeypatch, fact_count=0, existing=None)
    asyncio.run(dr._checkpoint_dossier(_STATE, _CFG))
    assert "save" not in calls            # Guard 1

def test_checkpoint_skips_existing_dossier(monkeypatch):
    calls = _setup(monkeypatch, fact_count=52, existing={"current_report": "established dossier"})
    asyncio.run(dr._checkpoint_dossier(_STATE, _CFG))
    assert "save" not in calls            # Guard 2

def test_checkpoint_skips_when_no_subject(monkeypatch):
    calls = _setup(monkeypatch, fact_count=52, existing=None)
    asyncio.run(dr._checkpoint_dossier({"prealloc_run_id": 7}, _CFG))
    assert "save" not in calls            # no subject -> no LLM resolution
```

- [ ] **Step 2: Run tests, verify fail**

Run: `.venv/bin/python -m pytest tests/test_partial_persist.py -v`
Expected: FAIL — `_checkpoint_dossier`/`_facts_report_md` undefined.

- [ ] **Step 3: Implement `_facts_report_md` + `_checkpoint_dossier`**

Add to `deep_researcher.py` just above `async def persist_research`:
```python
async def _facts_report_md(config, instance_key) -> str:
    """Render the facts gathered for an instance as `dossier show`-style markdown (NO LLM)."""
    import aiosqlite
    from open_deep_research.factbase import (query as _fbq, render as _fbr,
                                             schema as _fbschema, migrations as _fbmig)
    from open_deep_research.storage import _ensure_schema as _ens
    async with aiosqlite.connect(get_db_path(config)) as conn:
        await _ens(conn)
        await _fbmig.apply(conn, _fbschema.STEPS)
        grouped = await _fbq.FactQuery(conn).show_grouped(instance_key)
    return _fbr.render(grouped, fmt="md") if grouped else ""


async def _checkpoint_dossier(state, config) -> None:
    """Persist a PARTIAL subject dossier from the facts gathered so far (no LLM), so a
    whole-profile run that aborts/times out mid-loop still saves a usable dossier rather than
    nothing. Guards: requires an already-set subject (skip LLM resolution), fact_count>0, and a
    brand-new subject (never overwrites an existing established dossier). Best-effort."""
    try:
        subject = state.get("subject")
        if not subject:
            return
        db_path = get_db_path(config)
        prealloc = state.get("prealloc_run_id")
        fact_count = await _run_fact_count(db_path, prealloc) if prealloc else 0
        if fact_count <= 0:                                   # Guard 1
            return
        slug = slugify(subject)
        existing = await get_subject_by_slug(db_path, slug)
        if existing and existing.get("current_report"):       # Guard 2: don't poison existing
            return
        from open_deep_research.factbase import entities as _fbe
        ik = _fbe.CountryResolver().resolve_in_text(subject)
        if not ik:
            return
        report = await _facts_report_md(config, ik)
        if not report.strip():
            return
        now = datetime.now(timezone.utc).isoformat()
        sources = extract_sources(report)
        run = {
            "thread_id": (config.get("configurable") or {}).get("thread_id"),
            "topic": subject, "research_brief": state.get("research_brief"),
            "final_report": report, "sources": sources, "raw_notes": state.get("raw_notes", []),
            "config": {}, "status": "partial", "error": None, "created_at": now,
        }
        await save_run_and_upsert_subject(
            db_path, subject_name=subject, slug=slug, merged_report=report,
            sources_union=sources, run=run, now=now, run_id=prealloc)
        logger.info("Checkpointed partial dossier for %s (%d facts).", subject, fact_count)
    except Exception as e:  # noqa: BLE001 - best-effort; never fail the run on a checkpoint
        logger.warning("Partial-dossier checkpoint failed (non-fatal): %s", e)
```
(Verify the imports `_run_fact_count`, `get_subject_by_slug`, `save_run_and_upsert_subject`,
`slugify`, `extract_sources`, `datetime`, `get_db_path`, `logger` are already in scope in
`deep_researcher.py` — they are used by `persist_research`. If `get_subject_by_slug` /
`save_run_and_upsert_subject` aren't imported at module top, add them to the existing
`from open_deep_research.storage import ...` line.)

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/python -m pytest tests/test_partial_persist.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/deep_researcher.py tests/test_partial_persist.py
git commit -m "feat(dossier): _checkpoint_dossier — cheap no-LLM partial dossier persist"
```

---

### Task 2: Wire the checkpoint into `assess_completeness`

**Files:**
- Modify: `src/open_deep_research/deep_researcher.py` (`assess_completeness`, ~line 1924)
- Test: `tests/test_partial_persist.py`

**Interfaces:**
- Consumes: `_checkpoint_dossier` (Task 1).
- Produces: `assess_completeness` calls `_checkpoint_dossier(state, config)` after the subject→country resolution, before the loop/finalize routing.

- [ ] **Step 1: Write the failing test** (spy that assess_completeness invokes the checkpoint)

```python
# tests/test_partial_persist.py (add)
def test_assess_completeness_invokes_checkpoint(monkeypatch):
    seen = {}
    async def spy(state, config): seen["called"] = state.get("subject")
    monkeypatch.setattr(dr, "_checkpoint_dossier", spy)
    # resolve_in_text -> a country so assess_completeness proceeds past the early return
    import open_deep_research.factbase.entities as fbe
    monkeypatch.setattr(fbe.CountryResolver, "resolve_in_text", lambda self, t: "EST")
    # stub the DB-heavy completeness work so we only test the wiring: force the no-ik path off
    # by giving a subject; then let assess_completeness reach the checkpoint call.
    state = {"subject": "Estonia", "fact_rounds_used": 0, "raw_notes": [], "research_brief": "b"}
    cfg = {"configurable": {"thread_id": "t", "database_path": "/tmp/ac.db",
                            "whole_profile_mode": True, "profile_name": "country_digital_identity"}}
    try:
        asyncio.run(dr.assess_completeness(state, cfg))
    except Exception:
        pass  # downstream DB/profile work may error on an empty temp DB; we only assert the spy
    assert seen.get("called") == "Estonia"
```

- [ ] **Step 2: Run test, verify fail**

Run: `.venv/bin/python -m pytest tests/test_partial_persist.py::test_assess_completeness_invokes_checkpoint -v`
Expected: FAIL — `seen["called"]` not set (checkpoint not wired yet).

- [ ] **Step 3: Wire the call**

In `assess_completeness`, right after the `ik` resolution + the `if not ik:` early return (~line 1927), add the checkpoint call:
```python
    ik = fbentities.CountryResolver().resolve_in_text(subject) if subject else None
    if not ik:
        # Can't resolve subject to a country — go straight to terminal
        return Command(goto="synthesize_narrative", update={"fact_rounds_used": rounds_used})

    # Persist a partial dossier from the facts gathered so far (cheap, no LLM) BEFORE the
    # loop/finalize decision, so a run aborted/timed-out in a later gap round still saved a
    # usable dossier (the empty-dossier-on-timeout failure). Best-effort.
    await _checkpoint_dossier(state, config)
```

- [ ] **Step 4: Run test + the helper tests, verify pass**

Run: `.venv/bin/python -m pytest tests/test_partial_persist.py -v`
Expected: PASS (5 tests). The spy fires before the downstream completeness work.

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/deep_researcher.py tests/test_partial_persist.py
git commit -m "feat(dossier): checkpoint partial dossier each gap round in assess_completeness"
```

---

### Task 3: No-regression check + empirical validation

**Files:**
- Test: existing `tests/test_knowledge_flow.py` (persist path), plus a live re-run.

- [ ] **Step 1: Run the persist/graph suite (no regression)**

Run: `.venv/bin/python -m pytest tests/test_knowledge_flow.py tests/test_reaper_wiring.py tests/test_partial_persist.py -p no:warnings -q`
Expected: PASS. (`persist_research`/finalize behavior unchanged; the checkpoint is additive.)

- [ ] **Step 2: Graph compiles**

Run: `.venv/bin/python -c "import open_deep_research.deep_researcher as d; d._checkpoint_dossier; d.assess_completeness; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Empirical (the real proof) — partial dossier persists on a churned run**

Run a whole-profile Estonia dossier into an isolated DB with a SHORT timeout so it aborts mid-loop
(the exact failure we fixed):
```bash
rm -f /tmp/partial_eval.db
RESEARCH_DB_PATH=/tmp/partial_eval.db ANTHROPIC_API_KEY="" CLAUDE_USE_SUBSCRIPTION=true \
  ODR_PREFLIGHT=off MODEL_ROUTING_PRESET=agy timeout 900 \
  .venv/bin/python -m open_deep_research.factbase.dossier batch \
  --profile country_digital_identity --countries Estonia --concurrency 4 || true
RESEARCH_DB_PATH=/tmp/partial_eval.db .venv/bin/python -c \
  "import sqlite3;c=sqlite3.connect('/tmp/partial_eval.db');print('subjects:',c.execute('SELECT COUNT(*) FROM subjects').fetchone()[0],'| facts:',c.execute('SELECT COUNT(*) FROM fact').fetchone()[0])"
```
Expected: `subjects: 1` (a partial dossier persisted) with facts > 0 — vs `subjects: 0` before this
change. `dossier show Estonia` (with `RESEARCH_DB_PATH` set) renders it.

- [ ] **Step 4: Commit (if any harness tweak was needed; otherwise skip)**

No code change expected here — this task is verification. If the empirical run reveals a wiring gap,
return to Task 1/2.

---

## Self-Review

**Spec coverage:** §1 architecture (cheap checkpoint via existing `save_run_and_upsert_subject`,
wired into `assess_completeness`, finalize unchanged) → Tasks 1-2; §2 cheap report (render grouped
facts), Guard 1 (fact_count>0), Guard 2 (don't poison existing), `partial` status → Task 1; §3
testing (deterministic guard tests + empirical re-run) → Tasks 1-3. The spec's `_persist_subject`
factor-out is realized by reusing the existing `save_run_and_upsert_subject` primitive (noted in the
plan header) — no risky refactor of `persist_research`. All spec sections mapped.

**Placeholder scan:** No TBDs. Task 1 Step 3's parenthetical "verify imports in scope" is a concrete
grep-gated instruction (the names are all used by `persist_research`), not a placeholder.

**Type consistency:** `_facts_report_md(config, instance_key) -> str` and
`_checkpoint_dossier(state, config) -> None` are consistent across tasks; the `run` dict matches
`persist_research`'s shape (status `"partial"`); `save_run_and_upsert_subject(..., run_id=prealloc)`
matches the verified signature.

**Ordering:** 1 (helper) → 2 (wire into assess_completeness, uses Task 1) → 3 (verify + empirical).
