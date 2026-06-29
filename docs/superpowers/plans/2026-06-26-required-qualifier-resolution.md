# Required-Qualifier Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A fact lands its value *and* its required qualifiers (e.g. `data_protection_law=true` + `stage=in_force`) via explicit-capture → targeted-research → bounded-inference (marked, promotion-blocked), instead of churning forever as `missing_qualifier`.

**Architecture:** A new post-extraction graph node (`resolve_required_qualifiers`) inspects the run's facts, and for any fact missing a required qualifier resolves that axis from the fact's evidence span — *stated* if the source states it, *inferred* (marked, lower-trust) only after a targeted research round already tried, else *null* (left for research). Inferred qualifiers are recorded in a new `fact.qualifier_provenance_json` column and **blocked from promotion** to `trusted`. `assess_completeness` emits an axis-specific gap sub-query naming the missing qualifier + its enum options.

**Tech Stack:** Python 3.11, LangGraph, aiosqlite, pytest. Spec: `docs/superpowers/specs/2026-06-26-required-qualifier-resolution-design.md`.

## Global Constraints

- Tests run with `.venv/bin/python -m pytest <files> -p no:warnings -q -o addopts=""` (the project's own venv; bare `python` is not on PATH). Lint changed files with `uvx ruff check <file>`.
- The fact-base schema is at **version 10**; the new migration is **version 11** (append to `STEPS` in `src/open_deep_research/factbase/schema.py`; `migrations.apply` runs pending `(version, sql)` steps idempotently).
- Injected-`model_call` pattern (testable without a live model): mirror `nodes/extraction.py::_make_fact_model_call` — a sync factory returning an `async def model_call(...)`.
- Node best-effort discipline: a model error or `None` must leave facts unchanged and never fail the run (mirror `extract_facts`).
- The resolver only reads its own fact's evidence span; cross-source qualifier capture happens via targeted research feeding bulk extraction, NOT by the resolver reading another source.
- Inference is the LAST resort: only attempted for an axis already marked research-attempted in `state["qualifier_research_attempted"]`.

---

## File Structure

- `src/open_deep_research/factbase/schema.py` — add migration v11 (`fact.qualifier_provenance_json`).
- `src/open_deep_research/factbase/model.py` — `Fact` gains `has_inferred_required: bool`.
- `src/open_deep_research/factbase/promotion.py` — block promotion of inferred-required-qualifier facts.
- `src/open_deep_research/factbase/prompting.py` — `compile_property_catalog` marks `(REQUIRED)`.
- `src/open_deep_research/factbase/completeness.py` — `missing_required_qualifiers(grouped_rows, prof)` helper.
- `src/open_deep_research/factbase/qualifier_resolve.py` — **NEW** pure resolver: `resolve_qualifier(...)`.
- `src/open_deep_research/nodes/qualifiers.py` — **NEW** graph node `resolve_required_qualifiers` + `_make_qualifier_model_call`.
- `src/open_deep_research/nodes/completeness.py` — `assess_completeness` emits axis-aware directive + sets `qualifier_research_attempted`.
- `src/open_deep_research/state.py` — `AgentState.qualifier_research_attempted`.
- `src/open_deep_research/configuration.py` — `max_qualifier_resolutions`.
- `src/open_deep_research/deep_researcher.py` — wire the node on the `extract_facts → route_after_extract` edge + re-export.

---

### Task 1: Catalog marks required qualifiers `(REQUIRED)`

**Files:**
- Modify: `src/open_deep_research/factbase/prompting.py` (`compile_property_catalog`, the qualifier-rendering loop)
- Test: `tests/test_extraction_prompt.py`

**Interfaces:**
- Produces: `compile_property_catalog(prof, target_properties=None) -> str` now appends ` (REQUIRED)` after each qualifier listed in the property's `required_qualifiers`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_extraction_prompt.py
def test_catalog_marks_required_qualifiers():
    from open_deep_research.factbase.prompting import compile_property_catalog
    prof = fbprofile.load("country_digital_identity")  # data_protection_law requires `stage`
    cat = compile_property_catalog(prof, ["data_protection_law"])
    assert "stage=" in cat
    assert "(REQUIRED)" in cat                      # stage is marked required
    # a non-required qualifier on the same property is NOT marked
    assert "scope=" in cat and "scope=['comprehensive', 'sectoral'] (REQUIRED)" not in cat
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_extraction_prompt.py::test_catalog_marks_required_qualifiers -p no:warnings -q -o addopts=""`
Expected: FAIL (no `(REQUIRED)` in the compiled catalog).

- [ ] **Step 3: Implement**

In `compile_property_catalog`, where each property's qualifiers are rendered (the `stage=[...]; scope=[...]` segment), wrap required ones. Find the loop building the per-qualifier strings and change it to append `(REQUIRED)` when the qualifier name is in `pd.required_qualifiers`:

```python
        req = set(getattr(pd, "required_qualifiers", []) or [])
        qparts = []
        for q, allowed in (getattr(pd, "qualifier_enums", {}) or {}).items():
            tag = " (REQUIRED)" if q in req else ""
            qparts.append(f"{q}={allowed}{tag}")
        # ... join qparts with "; " into the existing "qualifiers: ..." segment
```

(Match the existing local variable names and join style in `compile_property_catalog`; only add the `req`/`tag` logic.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_extraction_prompt.py -p no:warnings -q -o addopts=""`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/prompting.py tests/test_extraction_prompt.py
git commit -m "feat(extract): mark required qualifiers in the compiled catalog"
```

---

### Task 2: Schema migration v11 — `fact.qualifier_provenance_json`

**Files:**
- Modify: `src/open_deep_research/factbase/schema.py` (append to `STEPS`)
- Test: `tests/test_qualifier_provenance_migration.py` (create)

**Interfaces:**
- Produces: `fact` table has a nullable `qualifier_provenance_json TEXT` column after migrations apply.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qualifier_provenance_migration.py
import asyncio
import aiosqlite
from open_deep_research.factbase import migrations, schema


def test_fact_has_qualifier_provenance_column():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await migrations.apply(conn, schema.STEPS)
            cur = await conn.execute("PRAGMA table_info(fact)")
            cols = {r[1] for r in await cur.fetchall()}
            assert "qualifier_provenance_json" in cols
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qualifier_provenance_migration.py -p no:warnings -q -o addopts=""`
Expected: FAIL (column absent).

- [ ] **Step 3: Implement**

Append to the `STEPS` list in `schema.py` (after the `(10, …)` entry):

```python
    (11, """
    ALTER TABLE fact ADD COLUMN qualifier_provenance_json TEXT;
    """),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qualifier_provenance_migration.py -p no:warnings -q -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/schema.py tests/test_qualifier_provenance_migration.py
git commit -m "feat(factbase): schema v11 adds fact.qualifier_provenance_json"
```

---

### Task 3: `Fact.has_inferred_required` + promotion block

**Files:**
- Modify: `src/open_deep_research/factbase/model.py` (`Fact`)
- Modify: `src/open_deep_research/factbase/promotion.py` (`evaluate`)
- Test: `tests/test_promotion_inferred.py` (create)

**Interfaces:**
- Consumes: `model.Fact` from Task baseline.
- Produces: `Fact` has `has_inferred_required: bool = False`; `promotion.evaluate(fact, bucket, has_open_conflict)` returns no `Promote` when `fact.has_inferred_required` is True.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_promotion_inferred.py
from open_deep_research.factbase import model, promotion


def _fact(**kw):
    base = dict(fact_id=1, tuple_key="t", as_of=None, value="true", unit=None,
                source_meets_bar=True, has_unspecified_required=False)
    base.update(kw)
    return model.Fact(**base)


def test_inferred_required_qualifier_blocks_promotion():
    f = _fact(has_inferred_required=True)
    assert promotion.evaluate(f, [f], has_open_conflict=False) is None  # not promoted


def test_stated_required_qualifier_still_promotes():
    f = _fact(has_inferred_required=False)
    assert isinstance(promotion.evaluate(f, [f], has_open_conflict=False), model.Promote)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_promotion_inferred.py -p no:warnings -q -o addopts=""`
Expected: FAIL (`Fact.__init__` has no `has_inferred_required`).

- [ ] **Step 3: Implement**

In `model.py`, add to `Fact` (after `narrative`):

```python
    # True when a REQUIRED qualifier on this fact was inferred (not stated) by the qualifier
    # resolver. Blocks promotion to 'trusted' so an inferred fact never renders as trusted.
    has_inferred_required: bool = False
```

In `promotion.py`, extend the eligibility line:

```python
    eligible = (fact.source_meets_bar and not fact.has_unspecified_required
                and not has_open_conflict and not fact.has_inferred_required)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_promotion_inferred.py -p no:warnings -q -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/model.py src/open_deep_research/factbase/promotion.py tests/test_promotion_inferred.py
git commit -m "feat(factbase): block promotion of inferred-required-qualifier facts"
```

---

### Task 4: `missing_required_qualifiers` completeness helper

**Files:**
- Modify: `src/open_deep_research/factbase/completeness.py`
- Test: `tests/test_factbase_completeness.py`

**Interfaces:**
- Consumes: `assess_property_status(grouped_rows, absent, prof)` (existing).
- Produces: `missing_required_qualifiers(grouped_rows, prof) -> dict[str, list[dict]]` returning, for each property whose status is `missing_qualifier`, the list of its absent required qualifiers with enum options: `{property_name: [{"qualifier": str, "enum": list[str]}, ...]}`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_factbase_completeness.py
from open_deep_research.factbase.completeness import missing_required_qualifiers

PROF_RQ = profile_from_dict({"entity_type": "country", "version": "1", "properties": [
    {"name": "dpl", "kind": "boolean",
     "identity_qualifiers": ["stage"], "required_qualifiers": ["stage"],
     "qualifier_enums": {"stage": ["enacted", "in_force"]}},
]})


def test_missing_required_qualifiers_names_axis_and_enum():
    # a value present but no `stage` qualifier -> missing_qualifier
    grouped = [{"property_name": "dpl", "value": "true", "admission": "trusted",
                "source_count": 2, "qualifiers": {}}]
    out = missing_required_qualifiers(grouped, PROF_RQ)
    assert out == {"dpl": [{"qualifier": "stage", "enum": ["enacted", "in_force"]}]}


def test_no_missing_required_qualifiers_when_present():
    grouped = [{"property_name": "dpl", "value": "true", "admission": "trusted",
                "source_count": 2, "qualifiers": {"stage": "in_force"}}]
    assert missing_required_qualifiers(grouped, PROF_RQ) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_factbase_completeness.py -p no:warnings -q -o addopts=""`
Expected: FAIL (ImportError: `missing_required_qualifiers`).

- [ ] **Step 3: Implement**

Add to `completeness.py`:

```python
def missing_required_qualifiers(grouped_rows, prof) -> dict:
    """For each property whose status is `missing_qualifier`, list its absent required
    qualifiers with enum options: {property_name: [{"qualifier", "enum"}, ...]}.

    Reuses `assess_property_status` for the status, then derives which required axes the
    chosen value row lacks. Properties that are resolved/missing_value/absent are omitted.
    """
    status = assess_property_status(grouped_rows, set(), prof)
    by_prop = {}
    for r in grouped_rows:
        by_prop.setdefault(r.get("property_name"), []).append(r)
    out = {}
    for pd in prof.properties:
        if status.get(pd.name) != "missing_qualifier":
            continue
        req = list(getattr(pd, "required_qualifiers", []) or [])
        enums = getattr(pd, "qualifier_enums", {}) or {}
        rows = by_prop.get(pd.name) or []
        present = set()
        for r in rows:
            present |= set((r.get("qualifiers") or {}).keys())
        absent = [{"qualifier": q, "enum": list(enums.get(q, []))} for q in req if q not in present]
        if absent:
            out[pd.name] = absent
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_factbase_completeness.py -p no:warnings -q -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/completeness.py tests/test_factbase_completeness.py
git commit -m "feat(factbase): missing_required_qualifiers helper (axis + enum)"
```

---

### Task 5: Pure resolver `resolve_qualifier`

**Files:**
- Create: `src/open_deep_research/factbase/qualifier_resolve.py`
- Test: `tests/test_qualifier_resolve.py` (create)

**Interfaces:**
- Produces: `async def resolve_qualifier(*, value, instance_name, property_name, qualifier, enum, evidence_span, allow_inference, model_call) -> dict | None`. `model_call(prompt: str) -> str` is injected (async) and returns the model's raw text; the function parses a JSON object `{"value": <token|null>, "basis": "stated"|"inferred"}`. Returns `{"value", "basis"}` with `value` a member of `enum`, or `None` (no usable answer). When `allow_inference` is False, a returned `basis == "inferred"` is downgraded to `None` (defer to research).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qualifier_resolve.py
import asyncio
from open_deep_research.factbase.qualifier_resolve import resolve_qualifier


def _mk(text):
    async def model_call(prompt):
        return text
    return model_call


def _call(text, allow_inference):
    return asyncio.run(resolve_qualifier(
        value="true", instance_name="Estonia", property_name="dpl",
        qualifier="stage", enum=["enacted", "in_force"], evidence_span="the Act is in force since 2019",
        allow_inference=allow_inference, model_call=_mk(text)))


def test_stated_qualifier_is_returned():
    assert _call('{"value": "in_force", "basis": "stated"}', allow_inference=False) == \
        {"value": "in_force", "basis": "stated"}


def test_inferred_deferred_when_inference_not_allowed():
    assert _call('{"value": "in_force", "basis": "inferred"}', allow_inference=False) is None


def test_inferred_returned_when_allowed():
    assert _call('{"value": "in_force", "basis": "inferred"}', allow_inference=True) == \
        {"value": "in_force", "basis": "inferred"}


def test_value_outside_enum_rejected():
    assert _call('{"value": "repealed", "basis": "stated"}', allow_inference=True) is None


def test_null_value_returns_none():
    assert _call('{"value": null}', allow_inference=True) is None


def test_unparseable_returns_none():
    assert _call('the model rambled with no json', allow_inference=True) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qualifier_resolve.py -p no:warnings -q -o addopts=""`
Expected: FAIL (module/function missing).

- [ ] **Step 3: Implement**

```python
# src/open_deep_research/factbase/qualifier_resolve.py
"""Resolve a single missing REQUIRED qualifier from a fact's own evidence span.

Pure + injected `model_call` so it is unit-testable without a live model. The model is
asked for the qualifier value as `stated` (in the source) or `inferred` (strongly implied);
inference is only honored when `allow_inference` is True (i.e. targeted research already ran).
"""
from __future__ import annotations

import json
from typing import Optional


def _first_json_object(text: str) -> Optional[dict]:
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        return obj if isinstance(obj, dict) else None
                    except Exception:  # noqa: BLE001
                        break
        start = text.find("{", start + 1)
    return None


def _build_prompt(*, value, instance_name, property_name, qualifier, enum, evidence_span,
                  allow_inference) -> str:
    mode = ("if the evidence explicitly states it return {\"value\": <token>, \"basis\": "
            "\"stated\"}; if it strongly implies it return {\"value\": <token>, \"basis\": "
            "\"inferred\"}; if neither, return {\"value\": null}."
            if allow_inference else
            "if the evidence explicitly states it return {\"value\": <token>, \"basis\": "
            "\"stated\"}; otherwise return {\"value\": null} (do not guess).")
    return (
        f"Property '{property_name}' (value '{value}') for {instance_name}.\n"
        f"Evidence: \"{evidence_span}\"\n"
        f"The required qualifier '{qualifier}' must be one of {enum}. {mode}\n"
        "Return only the JSON object."
    )


async def resolve_qualifier(*, value, instance_name, property_name, qualifier, enum,
                            evidence_span, allow_inference, model_call) -> Optional[dict]:
    prompt = _build_prompt(
        value=value, instance_name=instance_name, property_name=property_name,
        qualifier=qualifier, enum=enum, evidence_span=evidence_span,
        allow_inference=allow_inference)
    raw = await model_call(prompt)
    obj = _first_json_object(str(raw or ""))
    if not obj:
        return None
    val = obj.get("value")
    basis = obj.get("basis")
    if not val or val not in enum:
        return None
    if basis not in ("stated", "inferred"):
        return None
    if basis == "inferred" and not allow_inference:
        return None
    return {"value": val, "basis": basis}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qualifier_resolve.py -p no:warnings -q -o addopts=""`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/qualifier_resolve.py tests/test_qualifier_resolve.py
git commit -m "feat(factbase): pure resolve_qualifier (stated/inferred/null)"
```

---

### Task 6: `qualifier_research_attempted` state + config cap

**Files:**
- Modify: `src/open_deep_research/state.py` (`AgentState`)
- Modify: `src/open_deep_research/configuration.py`
- Test: `tests/test_qualifier_state.py` (create)

**Interfaces:**
- Produces: `AgentState.qualifier_research_attempted: Optional[list[str]]` (axis keys `"<property>::<qualifier>"`); `Configuration.max_qualifier_resolutions: int` (default 12).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qualifier_state.py
from open_deep_research.state import AgentState
from open_deep_research.configuration import Configuration


def test_state_has_qualifier_research_attempted():
    assert "qualifier_research_attempted" in AgentState.__annotations__


def test_config_has_max_qualifier_resolutions_default():
    c = Configuration()
    assert c.max_qualifier_resolutions == 12
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qualifier_state.py -p no:warnings -q -o addopts=""`
Expected: FAIL (annotation/field missing).

- [ ] **Step 3: Implement**

In `state.py`, in `AgentState` near `prev_incomplete_props`:

```python
    # Axis keys ("<property>::<qualifier>") for which a targeted research sub-query has been
    # emitted. Gates the resolver's last-resort inference (infer only after research was tried).
    qualifier_research_attempted: Optional[list[str]]
```

In `configuration.py`, add a field alongside `max_profile_rounds` (mirror its `Field` style):

```python
    max_qualifier_resolutions: int = Field(
        default=12,
        metadata={"x_oap_ui_config": {"type": "number", "default": 12,
            "description": "Hard cap on per-run required-qualifier resolver calls. Capped-out "
            "facts stay missing_qualifier and route to targeted research."}}
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qualifier_state.py -p no:warnings -q -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/state.py src/open_deep_research/configuration.py tests/test_qualifier_state.py
git commit -m "feat(state): qualifier_research_attempted + max_qualifier_resolutions"
```

---

### Task 7: `resolve_required_qualifiers` node

**Files:**
- Create: `src/open_deep_research/nodes/qualifiers.py`
- Test: `tests/test_resolve_required_qualifiers_node.py` (create)

**Interfaces:**
- Consumes: `resolve_qualifier` (Task 5); `Configuration`; the run's facts via aiosqlite; `state["qualifier_research_attempted"]`, `state["prealloc_run_id"]`, `state["subject"]`, profile via `_effective_profile_name`.
- Produces: `async def resolve_required_qualifiers(state, config) -> dict`. For each run fact with a value but a missing required qualifier: read its `evidence.quoted_span` (join by `fact_id`), call `resolve_qualifier` with `allow_inference = ("<prop>::<q>" in qualifier_research_attempted)`, and on a hit UPDATE `fact.qualifiers_json` (+ set `qualifier_provenance_json` and lower `confidence` when inferred) and write a `fact_revision`. Returns `{}` (best-effort) and logs a `stated/inferred/null` breakdown. Also exports `_make_qualifier_model_call(configurable, config)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_resolve_required_qualifiers_node.py
import asyncio, json, aiosqlite
from open_deep_research.factbase import migrations, schema
from open_deep_research.nodes.qualifiers import resolve_required_qualifiers


async def _seed_fact(db, *, qualifiers, run_id="t1"):
    async with aiosqlite.connect(db) as conn:
        await migrations.apply(conn, schema.STEPS)
        cur = await conn.execute(
            "INSERT INTO fact (property_name, instance_key, tuple_key, qualifiers_json, value, "
            "admission, lifecycle, run_id, created_at) VALUES "
            "('data_protection_law','EE','tk',?, 'true','provisional','current',?, '2026-06-26')",
            (json.dumps(qualifiers), run_id))
        fid = cur.lastrowid
        await conn.execute(
            "INSERT INTO evidence (fact_id, quoted_span, retrieved_at) VALUES (?,?,?)",
            (fid, "the Personal Data Protection Act is in force since 2019", "2026-06-26"))
        await conn.commit()
        return fid


def _state(db, attempted):
    return {"prealloc_run_id": "t1", "subject": "Estonia",
            "qualifier_research_attempted": attempted,
            "_test_db": db}  # the node reads get_db_path(config); see Step 3 note


def test_resolver_fills_stated_qualifier(tmp_path, monkeypatch):
    db = str(tmp_path / "f.db")
    fid = asyncio.run(_seed_fact(db, qualifiers={}))

    async def fake_mc(prompt):
        return '{"value": "in_force", "basis": "stated"}'
    monkeypatch.setattr("open_deep_research.nodes.qualifiers._make_qualifier_model_call",
                        lambda c, cfg: fake_mc)
    cfg = {"configurable": {"thread_id": "t1", "database_path": db,
                            "whole_profile_mode": True, "profile_name": "country_digital_identity"}}
    state = {"prealloc_run_id": "t1", "subject": "Estonia", "qualifier_research_attempted": []}
    asyncio.run(resolve_required_qualifiers(state, cfg))

    async def read():
        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute("SELECT qualifiers_json, qualifier_provenance_json FROM fact WHERE id=?", (fid,))
            return await cur.fetchone()
    q, prov = asyncio.run(read())
    assert json.loads(q).get("stage") == "in_force"
    assert prov is None  # stated -> no inferred-provenance marker


def test_resolver_defers_inference_until_research_attempted(tmp_path, monkeypatch):
    db = str(tmp_path / "f.db")
    fid = asyncio.run(_seed_fact(db, qualifiers={}))

    async def fake_mc(prompt):
        # model would infer, but allow_inference must be False -> resolve_qualifier returns None
        return '{"value": "in_force", "basis": "inferred"}'
    monkeypatch.setattr("open_deep_research.nodes.qualifiers._make_qualifier_model_call",
                        lambda c, cfg: fake_mc)
    cfg = {"configurable": {"thread_id": "t1", "database_path": db,
                            "whole_profile_mode": True, "profile_name": "country_digital_identity"}}
    state = {"prealloc_run_id": "t1", "subject": "Estonia", "qualifier_research_attempted": []}
    asyncio.run(resolve_required_qualifiers(state, cfg))

    async def read():
        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute("SELECT qualifiers_json FROM fact WHERE id=?", (fid,))
            return await cur.fetchone()
    (q,) = asyncio.run(read())
    assert "stage" not in json.loads(q)  # deferred, not inferred


def test_resolver_infers_when_research_attempted(tmp_path, monkeypatch):
    db = str(tmp_path / "f.db")
    fid = asyncio.run(_seed_fact(db, qualifiers={}))

    async def fake_mc(prompt):
        return '{"value": "in_force", "basis": "inferred"}'
    monkeypatch.setattr("open_deep_research.nodes.qualifiers._make_qualifier_model_call",
                        lambda c, cfg: fake_mc)
    cfg = {"configurable": {"thread_id": "t1", "database_path": db,
                            "whole_profile_mode": True, "profile_name": "country_digital_identity"}}
    state = {"prealloc_run_id": "t1", "subject": "Estonia",
             "qualifier_research_attempted": ["data_protection_law::stage"]}
    asyncio.run(resolve_required_qualifiers(state, cfg))

    async def read():
        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute("SELECT qualifiers_json, qualifier_provenance_json FROM fact WHERE id=?", (fid,))
            return await cur.fetchone()
    q, prov = asyncio.run(read())
    assert json.loads(q).get("stage") == "in_force"
    assert json.loads(prov).get("stage") == "inferred"  # inferred -> marked
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_resolve_required_qualifiers_node.py -p no:warnings -q -o addopts=""`
Expected: FAIL (module/node missing).

- [ ] **Step 3: Implement**

```python
# src/open_deep_research/nodes/qualifiers.py
"""Post-extraction node: resolve facts stuck as missing a REQUIRED qualifier.

For each run fact with a value but an absent required qualifier, resolve that axis from the
fact's own evidence span (stated, or inferred only after research was attempted). Best-effort:
errors leave facts unchanged. See spec 2026-06-26-required-qualifier-resolution-design.md.
"""
from __future__ import annotations

import json
import logging

import aiosqlite
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from open_deep_research.configuration import Configuration
from open_deep_research.state import AgentState
from open_deep_research.storage import get_db_path
from open_deep_research.utils import get_api_key_for_model
from open_deep_research.nodes.profiles import _effective_profile_name
from open_deep_research.nodes.extraction import configurable_model

logger = logging.getLogger(__name__)

_INFERRED_CONFIDENCE = 0.5  # recorded for a future precedence project; inert in v1


def _make_qualifier_model_call(configurable, config):
    """Async model_call(prompt) -> raw text for resolve_qualifier (routable to a strong model)."""
    async def model_call(prompt: str) -> str:
        model = (
            configurable_model
            .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
            .with_config({
                "model": configurable.model_for("extract_facts", "researcher"),
                "model_chain": configurable.model_chain("researcher", "extract_facts"),
                "stage": "extract_facts",
                "max_tokens": configurable.researcher_model_max_tokens,
                "api_key": get_api_key_for_model(configurable.researcher_model, config),
                "tags": ["langsmith:nostream"],
            })
        )
        resp = await model.ainvoke([HumanMessage(content=prompt)])
        return str(getattr(resp, "content", "") or "")
    return model_call


async def resolve_required_qualifiers(state: AgentState, config: RunnableConfig) -> dict:
    configurable = Configuration.from_runnable_config(config)
    if not configurable.persist_results:
        return {}
    run_id = state.get("prealloc_run_id")
    if not run_id:
        return {}
    from open_deep_research.factbase import profile as fbprofile, qualifier_resolve, migrations, schema
    try:
        prof = fbprofile.load(_effective_profile_name(state, configurable))
    except Exception as e:  # noqa: BLE001
        logger.warning("qualifier resolver: profile load failed (non-fatal): %s", e)
        return {}
    attempted = set(state.get("qualifier_research_attempted") or [])
    model_call = _make_qualifier_model_call(configurable, config)
    cap = configurable.max_qualifier_resolutions
    counts = {"stated": 0, "inferred": 0, "null": 0}
    calls = 0

    async with aiosqlite.connect(get_db_path(config)) as conn:
        await migrations.apply(conn, schema.STEPS)
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT f.id, f.property_name, f.instance_key, f.value, f.qualifiers_json, "
            "e.quoted_span FROM fact f LEFT JOIN evidence e ON e.fact_id = f.id "
            "WHERE f.run_id = ? AND f.soft_deleted_at IS NULL", (str(run_id),))
        rows = await cur.fetchall()
        for row in rows:
            if calls >= cap:
                logger.info("qualifier resolver hit cap (%d); remaining facts route to research", cap)
                break
            try:
                pd = prof.property(row["property_name"])
            except KeyError:
                continue
            req = list(getattr(pd, "required_qualifiers", []) or [])
            if not req:
                continue
            quals = json.loads(row["qualifiers_json"] or "{}")
            span = row["quoted_span"]
            if not span:
                continue
            enums = getattr(pd, "qualifier_enums", {}) or {}
            for q in req:
                if quals.get(q):
                    continue  # already present
                allow = f"{row['property_name']}::{q}" in attempted
                calls += 1
                try:
                    res = await qualifier_resolve.resolve_qualifier(
                        value=row["value"], instance_name=row["instance_key"],
                        property_name=row["property_name"], qualifier=q,
                        enum=list(enums.get(q, [])), evidence_span=span,
                        allow_inference=allow, model_call=model_call)
                except Exception as e:  # noqa: BLE001
                    logger.warning("qualifier resolver call failed (non-fatal): %s", e)
                    res = None
                if not res:
                    counts["null"] += 1
                    continue
                counts[res["basis"]] += 1
                quals[q] = res["value"]
                inferred = res["basis"] == "inferred"
                prov = json.loads(row["qualifier_provenance_json"] or "{}") if "qualifier_provenance_json" in row.keys() else {}
                if inferred:
                    prov[q] = "inferred"
                await conn.execute(
                    "UPDATE fact SET qualifiers_json=?, qualifier_provenance_json=?, "
                    "confidence=COALESCE(?, confidence) WHERE id=?",
                    (json.dumps(quals), json.dumps(prov) if prov else None,
                     _INFERRED_CONFIDENCE if inferred else None, row["id"]))
                await conn.execute(
                    "INSERT INTO fact_revision (fact_id, change, cause, why, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (row["id"], f"{q}={res['value']} ({res['basis']})",
                     "qualifier_resolve", "required qualifier resolved", "2026-06-26"))
        await conn.commit()
    logger.info("qualifier resolver: stated=%(stated)d inferred=%(inferred)d null=%(null)d", counts)
    return {}
```

Note: the test's `monkeypatch` replaces `_make_qualifier_model_call`, so the live model is never called; `created_at` uses a fixed literal because `Date.now()`-style calls aren't needed here. The node reads the DB via `get_db_path(config)` (the test passes `database_path`).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_resolve_required_qualifiers_node.py -p no:warnings -q -o addopts=""`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/nodes/qualifiers.py tests/test_resolve_required_qualifiers_node.py
git commit -m "feat(nodes): resolve_required_qualifiers node (stated/defer/infer)"
```

---

### Task 8: Mark `has_inferred_required` at promotion time

**Files:**
- Modify: `src/open_deep_research/factbase/ingest.py` (the bucket loop building `model.Fact` for `promotion.evaluate`, ~line 90-126)
- Test: `tests/test_ingest_inferred_promotion.py` (create)

**Interfaces:**
- Consumes: `fact.qualifier_provenance_json` (Task 2/7); `Fact.has_inferred_required` (Task 3).
- Produces: when ingest re-evaluates promotion for a fact whose stored `qualifier_provenance_json` marks any required qualifier `inferred`, the constructed `model.Fact` has `has_inferred_required=True` so it is not promoted.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ingest_inferred_promotion.py
import json
from open_deep_research.factbase.promotion import has_inferred_required_qualifier


def test_inferred_provenance_sets_flag():
    assert has_inferred_required_qualifier(json.dumps({"stage": "inferred"})) is True


def test_stated_or_empty_provenance_does_not():
    assert has_inferred_required_qualifier(json.dumps({"stage": "stated"})) is False
    assert has_inferred_required_qualifier("{}") is False
    assert has_inferred_required_qualifier(None) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ingest_inferred_promotion.py -p no:warnings -q -o addopts=""`
Expected: FAIL (ImportError: `has_inferred_required_qualifier`).

- [ ] **Step 3: Implement**

Add the shared predicate to `factbase/promotion.py`:

```python
def has_inferred_required_qualifier(qualifier_provenance_json: str | None) -> bool:
    """True if any qualifier in the provenance JSON is marked `inferred`."""
    import json
    prov = json.loads(qualifier_provenance_json or "{}")
    return any(v == "inferred" for v in prov.values())
```

Then, in `ingest.py` and `rebuild.py`, wherever a `model.Fact` is built for `promotion.evaluate`, select the row's `qualifier_provenance_json` and pass
`has_inferred_required=promotion.has_inferred_required_qualifier(prov_json)` into the `Fact(...)` constructor (it defaults to `False`, so untouched paths are unaffected).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_ingest_inferred_promotion.py -p no:warnings -q -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/promotion.py src/open_deep_research/factbase/ingest.py src/open_deep_research/factbase/rebuild.py tests/test_ingest_inferred_promotion.py
git commit -m "feat(factbase): carry inferred-required flag into promotion"
```

---

### Task 9: `assess_completeness` emits axis-aware directive + research-attempted

**Files:**
- Modify: `src/open_deep_research/nodes/completeness.py` (`assess_completeness`)
- Test: `tests/test_gaploop_bailout.py` (or `tests/test_factbase_completeness.py` for the directive string)

**Interfaces:**
- Consumes: `missing_required_qualifiers` (Task 4); `state["qualifier_research_attempted"]`.
- Produces: when a property is `missing_qualifier`, the gap directive names the specific axis + enum; the Command update adds `qualifier_research_attempted` (union of prior + the emitted axes).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_factbase_completeness.py
def test_axis_directive_text_built_from_missing():
    from open_deep_research.nodes.completeness import _qualifier_gap_directive
    mrq = {"data_protection_law": [{"qualifier": "stage", "enum": ["enacted", "in_force"]}]}
    text, axes = _qualifier_gap_directive(mrq)
    assert "data_protection_law" in text and "stage" in text and "in_force" in text
    assert "primary" in text.lower()
    assert axes == ["data_protection_law::stage"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_factbase_completeness.py::test_axis_directive_text_built_from_missing -p no:warnings -q -o addopts=""`
Expected: FAIL (`_qualifier_gap_directive` missing).

- [ ] **Step 3: Implement**

Add a pure helper in `nodes/completeness.py`:

```python
def _qualifier_gap_directive(missing_rq: dict) -> tuple[str, list[str]]:
    """Build the axis-aware gap directive + the list of "<prop>::<qualifier>" axes it targets."""
    lines, axes = [], []
    for prop, items in missing_rq.items():
        for it in items:
            q, enum = it["qualifier"], it["enum"]
            lines.append(
                f"{prop}: the value is known, but its required '{q}' ({' vs '.join(enum)}) is "
                f"unconfirmed -- find a PRIMARY/official source (statute, act, or regulator) "
                f"stating it.")
            axes.append(f"{prop}::{q}")
    return ("\n".join(lines), axes)
```

In `assess_completeness`, after computing `incomplete` and choosing the gap round (`goto == "write_research_brief"`), compute `mrq = fbc.missing_required_qualifiers(grouped, prof)`, build `qtext, qaxes = _qualifier_gap_directive(mrq)`, append `qtext` to the `gap` string when non-empty, and add to the Command update:

```python
                    "qualifier_research_attempted": sorted(
                        set(state.get("qualifier_research_attempted") or []) | set(qaxes)),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_factbase_completeness.py tests/test_gaploop_bailout.py -p no:warnings -q -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/nodes/completeness.py tests/test_factbase_completeness.py
git commit -m "feat(dossier): axis-aware qualifier gap directive + research-attempted state"
```

---

### Task 10: Wire `resolve_required_qualifiers` into the graph

**Files:**
- Modify: `src/open_deep_research/deep_researcher.py` (import + re-export + node + edges)
- Test: `tests/test_graph_identity.py` (update the snapshot) + a wiring assertion

**Interfaces:**
- Consumes: `resolve_required_qualifiers` (Task 7).
- Produces: graph has node `resolve_required_qualifiers`; the `extract_facts → route_after_extract` conditional edge is replaced by `extract_facts → resolve_required_qualifiers → route_after_extract`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_graph_identity.py
def test_resolve_required_qualifiers_node_present():
    from open_deep_research.deep_researcher import deep_researcher as g
    assert "resolve_required_qualifiers" in g.get_graph().nodes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_graph_identity.py::test_resolve_required_qualifiers_node_present -p no:warnings -q -o addopts=""`
Expected: FAIL (node absent).

- [ ] **Step 3: Implement**

In `deep_researcher.py`: import + re-export the node, add it, and re-route. Add to the `nodes.qualifiers` import block and `__all__`:

```python
from open_deep_research.nodes.qualifiers import resolve_required_qualifiers
```
```python
    "resolve_required_qualifiers",
```

Add the node next to `extract_facts`:

```python
deep_researcher_builder.add_node("resolve_required_qualifiers", resolve_required_qualifiers)
```

Replace the existing `add_conditional_edges("extract_facts", route_after_extract, {...})` block: first add a static edge `extract_facts → resolve_required_qualifiers`, then move the conditional edges to originate from the new node:

```python
deep_researcher_builder.add_edge("extract_facts", "resolve_required_qualifiers")
deep_researcher_builder.add_conditional_edges(
    "resolve_required_qualifiers", route_after_extract,
    {"persist_research": "persist_research", "assess_sufficiency": "assess_sufficiency",
     "assess_completeness": "assess_completeness"})
```

Then regenerate the graph-identity snapshot per the snapshot test's documented procedure (the test file explains how the `EXPECTED_NODES`/edge set is captured) and update it to include the new node + rerouted edges.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_graph_identity.py -p no:warnings -q -o addopts=""`
Expected: PASS (snapshot + presence test).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/deep_researcher.py tests/test_graph_identity.py
git commit -m "feat(graph): wire resolve_required_qualifiers between extract and routing"
```

---

### Task 11: End-to-end resolution test (either lever)

**Files:**
- Test: `tests/test_qualifier_resolution_e2e.py` (create)

**Interfaces:**
- Consumes: the full node + storage path (Tasks 2,3,5,7).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qualifier_resolution_e2e.py
import asyncio, json, aiosqlite
from open_deep_research.factbase import migrations, schema
from open_deep_research.nodes.qualifiers import resolve_required_qualifiers


def test_property_resolves_by_either_lever(tmp_path, monkeypatch):
    """The required qualifier ends up present (resolved); IF inferred, provenance is stamped.
    Does NOT assert that inference specifically wins (non-deterministic / priority-dependent)."""
    db = str(tmp_path / "f.db")

    async def seed():
        async with aiosqlite.connect(db) as conn:
            await migrations.apply(conn, schema.STEPS)
            cur = await conn.execute(
                "INSERT INTO fact (property_name, instance_key, tuple_key, qualifiers_json, value, "
                "admission, lifecycle, run_id, created_at) VALUES "
                "('data_protection_law','EE','tk','{}','true','provisional','current','t1','2026-06-26')")
            fid = cur.lastrowid
            await conn.execute("INSERT INTO evidence (fact_id, quoted_span, retrieved_at) VALUES (?,?,?)",
                               (fid, "the Act is in force since 2019", "2026-06-26"))
            await conn.commit()
            return fid
    fid = asyncio.run(seed())

    async def fake_mc(prompt):
        return '{"value": "in_force", "basis": "stated"}'
    monkeypatch.setattr("open_deep_research.nodes.qualifiers._make_qualifier_model_call",
                        lambda c, cfg: fake_mc)
    cfg = {"configurable": {"thread_id": "t1", "database_path": db,
                            "whole_profile_mode": True, "profile_name": "country_digital_identity"}}
    state = {"prealloc_run_id": "t1", "subject": "Estonia", "qualifier_research_attempted": []}
    asyncio.run(resolve_required_qualifiers(state, cfg))

    async def read():
        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute("SELECT qualifiers_json, qualifier_provenance_json FROM fact WHERE id=?", (fid,))
            return await cur.fetchone()
    q, prov = asyncio.run(read())
    assert json.loads(q).get("stage") == "in_force"          # resolved
    if prov:                                                  # iff inferred, provenance stamped
        assert json.loads(prov).get("stage") == "inferred"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qualifier_resolution_e2e.py -p no:warnings -q -o addopts=""`
Expected: FAIL before Tasks 2/5/7 land; PASS after.

- [ ] **Step 3: Implement**

No new code — this test exercises the assembled path. If it fails, fix the responsible task, not the test.

- [ ] **Step 4: Run the GUARD set**

Run: `.venv/bin/python -m pytest tests/test_qualifier_resolve.py tests/test_resolve_required_qualifiers_node.py tests/test_factbase_completeness.py tests/test_gaploop_bailout.py tests/test_promotion_inferred.py tests/test_graph_identity.py tests/test_qualifier_resolution_e2e.py -p no:warnings -q -o addopts=""`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_qualifier_resolution_e2e.py
git commit -m "test(dossier): required-qualifier resolves by either lever (e2e)"
```

---

## Self-Review

**Spec coverage:** §1 catalog `(REQUIRED)` → Task 1; §2 resolver node → Tasks 5+7; §3 evidence-span join → Task 7 (LEFT JOIN evidence); cross-source handled by research-preferred (Tasks 7+9, no resolver-reading); §4 axis-aware directive + research-attempted → Tasks 4+9; §5 promotion block → Tasks 3+8; §6 provenance column live + confidence inert → Tasks 2+7; §7 completeness integration (resolver before check) → Task 10 ordering; decisions (research-preferred / inference-last) → Task 5 `allow_inference` gate + Task 9 axis bookkeeping + Task 7 node gate; error handling (best-effort, cap, skip) → Task 7; observability (stated/inferred/null log) → Task 7; testing (resolves by either lever) → Task 11. All spec sections mapped.

**Placeholder scan:** none. Task 10 Step 3 references the snapshot procedure documented in `tests/test_graph_identity.py` (a concrete in-repo procedure, not a placeholder) and Task 8's test is finalized in Step 3 to import the shared predicate.

**Type consistency:** `resolve_qualifier(...)` keyword args + `{"value", "basis"}` return are identical across Tasks 5/7/11; `has_inferred_required` (Task 3) is read via `has_inferred_required_qualifier(...)` (Task 8) and consumed in `promotion.evaluate` (Task 3); `qualifier_research_attempted` axis-key format `"<property>::<qualifier>"` is identical in Tasks 6/7/9; `qualifier_provenance_json` value shape `{"<qualifier>": "inferred"}` is identical in Tasks 7/8/11; `missing_required_qualifiers` return shape `{prop: [{"qualifier","enum"}]}` is identical in Tasks 4/9.

**Ordering:** 1 catalog → 2 migration → 3 model+promotion → 4 completeness helper → 5 pure resolver → 6 state+config → 7 node → 8 promotion-mark wiring → 9 completeness directive → 10 graph wiring → 11 e2e. Every task's dependencies precede it.
