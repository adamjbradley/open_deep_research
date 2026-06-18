# Profile-Driven Completeness & Narrative Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the profile YAML the single definition of a complete, narrated dossier — every property resolved (value + required qualifiers + required narrative) or confirmed-absent, plus a profile-defined subject narrative.

**Architecture:** Approach A — extend the hardened facts-first loop into a whole-profile checklist. New per-property `narrative`/`completeness` YAML fields steer retrieval+extraction (via the existing catalog) and a deterministic status ledger; a `property_status` table records confirmed-absences; an `assess_completeness` node drives the loop to resolved-or-absent; a `synthesize_narrative` node writes the subject dossier.

**Tech Stack:** Python 3.11, pydantic v2, aiosqlite, LangGraph, pytest. Spec: `docs/superpowers/specs/2026-06-18-profile-driven-completeness-design.md`.

## Global Constraints

- Tests run with `.venv/bin/python -m pytest` (bare `python` is not on PATH).
- Already on branch `harden-routing-failover`; do NOT branch or touch main.
- **Back-compatibility is mandatory:** a profile with none of the new fields, and a run with the new mode off, must behave exactly as today. New behavior is gated by the new fields / a config flag.
- New YAML fields are all optional with today's behavior as the default.
- Best-effort LLM nodes never abort a run — they fall back to deterministic behavior on any error.
- Follow existing factbase patterns (pydantic `ProfileModel` -> dataclass `PropertyDef` via `profile_from_dict`; migrations as `(version, sql)` tuples in `schema.STEPS`).

---

### Task 1: Profile schema — narrative, completeness, overview_sections

**Files:**
- Modify: `src/open_deep_research/factbase/profile_schema.py` (`PropertyModel`, `ProfileModel`, `profile_from_dict`)
- Modify: `src/open_deep_research/factbase/profile.py` (`PropertyDef`, `Profile`)
- Test: `tests/test_factbase_profile_schema.py`, `tests/test_factbase_profile.py`

**Interfaces:**
- Produces: `PropertyDef.narrative_required: bool`, `PropertyDef.narrative_guidance: str`, `PropertyDef.completeness: str` (`"required"`|`"optional"`, default `"required"`), `PropertyDef.absence_allowed: bool` (default `True`); `Profile.overview_sections: list[str]`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_factbase_profile_schema.py (add)
from open_deep_research.factbase.profile_schema import profile_from_dict

def test_profile_parses_narrative_and_completeness_fields():
    prof = profile_from_dict({
        "entity_type": "country", "version": "1",
        "narrative": {"overview_sections": ["How it works", "Coverage gaps"]},
        "properties": [
            {"name": "scheme", "kind": "name",
             "narrative": {"required": True, "guidance": "Explain enrolment + caveats."},
             "completeness": "required", "absence_allowed": False},
            {"name": "bio", "kind": "enum", "value_enum": ["photo"], "multi": True},
        ],
    })
    p = prof.property("scheme")
    assert p.narrative_required is True
    assert "enrolment" in p.narrative_guidance
    assert p.completeness == "required" and p.absence_allowed is False
    assert prof.overview_sections == ["How it works", "Coverage gaps"]
    # defaults when omitted:
    b = prof.property("bio")
    assert b.narrative_required is False and b.completeness == "required" and b.absence_allowed is True

def test_back_compat_profile_without_new_fields():
    prof = profile_from_dict({"entity_type": "country", "version": "1",
                              "properties": [{"name": "x", "kind": "name"}]})
    assert prof.overview_sections == []
    assert prof.property("x").narrative_required is False
```

- [ ] **Step 2: Run tests, verify fail**

Run: `.venv/bin/python -m pytest tests/test_factbase_profile_schema.py -k "narrative or back_compat" -v`
Expected: FAIL — `AttributeError`/unexpected-field.

- [ ] **Step 3: Add the pydantic models (profile_schema.py)**

In `PropertyModel` add fields:
```python
    narrative: Optional[dict] = None          # {"required": bool, "guidance": str}
    completeness: str = "required"            # "required" | "optional"
    absence_allowed: bool = True
```
In `PropertyModel._check` (after the existing checks) add:
```python
        if self.completeness not in ("required", "optional"):
            raise ValueError(f"property {self.name!r}: completeness must be 'required' or 'optional'")
        if self.narrative is not None and not isinstance(self.narrative, dict):
            raise ValueError(f"property {self.name!r}: narrative must be a mapping")
```
In `ProfileModel` add:
```python
    narrative: Optional[dict] = None          # {"overview_sections": [str, ...]}
```

- [ ] **Step 4: Add dataclass fields + mapping (profile.py)**

In `PropertyDef` (after `open_world`):
```python
    narrative_required: bool = False
    narrative_guidance: str = ""
    completeness: str = "required"
    absence_allowed: bool = True
```
In `Profile` add a field `overview_sections: list[str] = field(default_factory=list)`.

In `profile_from_dict` (profile_schema.py), map per property:
```python
            narrative_required=bool((p.narrative or {}).get("required", False)),
            narrative_guidance=str((p.narrative or {}).get("guidance", "") or ""),
            completeness=p.completeness,
            absence_allowed=p.absence_allowed,
```
and on the `Profile(...)` construction:
```python
        overview_sections=list((data.get("narrative") or {}).get("overview_sections", []) or []),
```
(Confirm the `ProfileModel`/`Profile` construction site; `data` is the raw dict — read `narrative.overview_sections` from it or from the validated `ProfileModel.narrative`.)

- [ ] **Step 5: Run tests, verify pass**

Run: `.venv/bin/python -m pytest tests/test_factbase_profile_schema.py tests/test_factbase_profile.py tests/test_factbase_profile_roundtrip.py -v`
Expected: PASS (including existing roundtrip — if the roundtrip serializer enumerates fields, add the new ones to it).

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/factbase/profile_schema.py src/open_deep_research/factbase/profile.py tests/test_factbase_profile_schema.py tests/test_factbase_profile.py
git commit -m "feat(factbase): profile narrative/completeness/overview_sections schema"
```

---

### Task 2: Compile narrative guidance into the catalog (steers retrieval + extraction)

**Files:**
- Modify: `src/open_deep_research/factbase/prompting.py` (`compile_property_catalog`)
- Test: `tests/test_factbase_prompting.py`

**Interfaces:**
- Consumes: `PropertyDef.narrative_required`, `PropertyDef.narrative_guidance` (Task 1).
- Produces: the compiled catalog line for a property includes a `narrative:` clause when `narrative_required`.

- [ ] **Step 1: Write failing test**

```python
# tests/test_factbase_prompting.py (add)
def test_catalog_includes_narrative_guidance():
    from open_deep_research.factbase.profile_schema import profile_from_dict
    prof = profile_from_dict({"entity_type": "country", "version": "1", "properties": [
        {"name": "scheme", "kind": "name",
         "narrative": {"required": True, "guidance": "Explain enrolment and caveats."}},
    ]})
    cat = compile_property_catalog(prof, ["scheme"])
    assert "narrative" in cat.lower()
    assert "enrolment" in cat
```

- [ ] **Step 2: Run test, verify fail**

Run: `.venv/bin/python -m pytest tests/test_factbase_prompting.py -k narrative_guidance -v`
Expected: FAIL — guidance not in catalog.

- [ ] **Step 3: Emit the clause in `compile_property_catalog`**

After the qualifiers clause is appended to `line`, add:
```python
        if getattr(pd, "narrative_required", False) and getattr(pd, "narrative_guidance", ""):
            line += f" | narrative (required): {pd.narrative_guidance}"
```

- [ ] **Step 4: Run test, verify pass**

Run: `.venv/bin/python -m pytest tests/test_factbase_prompting.py -v`
Expected: PASS. (The catalog already flows into both `_steer_brief_with_catalog` and `build_extraction_prompt`, so no other change is needed for steering.)

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/prompting.py tests/test_factbase_prompting.py
git commit -m "feat(factbase): compile narrative guidance into the property catalog"
```

---

### Task 3: `property_status` table + store

**Files:**
- Modify: `src/open_deep_research/factbase/schema.py` (`STEPS`)
- Create: `src/open_deep_research/factbase/property_status.py`
- Test: `tests/test_factbase_property_status.py`

**Interfaces:**
- Produces: `PropertyStatusStore(conn)` with `async def record_absent(instance_key, property_name, qualifiers: dict, evidence: str, run_id: int|None, as_of: str|None)` and `async def absent_properties(instance_key) -> set[str]`.

- [ ] **Step 1: Add the migration step (schema.py)**

Append a new `(version, sql)` tuple to `STEPS` (use the next version integer after the current max):
```python
    (NEXT_VERSION, """
        CREATE TABLE IF NOT EXISTS property_status (
            id INTEGER PRIMARY KEY,
            instance_key TEXT NOT NULL,
            property_name TEXT NOT NULL,
            qualifiers_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL,
            evidence TEXT,
            run_id INTEGER,
            as_of TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE INDEX IF NOT EXISTS ix_property_status_instance
            ON property_status(instance_key, property_name);
    """),
```
(Read the current last version number in `STEPS` and use the next integer; keep the existing split-on-`;` convention.)

- [ ] **Step 2: Write failing test**

```python
# tests/test_factbase_property_status.py (new)
import aiosqlite, pytest
from open_deep_research.factbase import migrations as fbmig, schema as fbschema
from open_deep_research.factbase.property_status import PropertyStatusStore

@pytest.mark.asyncio
async def test_record_and_read_absent(tmp_path):
    db = str(tmp_path / "t.db")
    async with aiosqlite.connect(db) as conn:
        await fbmig.apply(conn, fbschema.STEPS)
        store = PropertyStatusStore(conn)
        await store.record_absent("EST", "biometric_capture", {}, "searched 5 sources; none state biometrics", 1, None)
        await conn.commit()
        assert await store.absent_properties("EST") == {"biometric_capture"}
        assert await store.absent_properties("IND") == set()
```
(If the suite isn't configured for `pytest.mark.asyncio`, follow the async-test pattern already used in `tests/test_factbase_store.py`.)

- [ ] **Step 3: Run test, verify fail**

Run: `.venv/bin/python -m pytest tests/test_factbase_property_status.py -v`
Expected: FAIL — module missing.

- [ ] **Step 4: Implement `property_status.py`**

```python
"""Per-property research status (currently: confirmed-absent records)."""
from __future__ import annotations
import json

class PropertyStatusStore:
    def __init__(self, conn):
        self._conn = conn

    async def record_absent(self, instance_key, property_name, qualifiers, evidence, run_id, as_of):
        await self._conn.execute(
            "INSERT INTO property_status (instance_key, property_name, qualifiers_json, status, "
            "evidence, run_id, as_of) VALUES (?,?,?,?,?,?,?)",
            (instance_key, property_name, json.dumps(qualifiers or {}, sort_keys=True),
             "confirmed_absent", evidence, run_id, as_of),
        )

    async def absent_properties(self, instance_key) -> set:
        cur = await self._conn.execute(
            "SELECT DISTINCT property_name FROM property_status "
            "WHERE instance_key=? AND status='confirmed_absent'", (instance_key,))
        return {r[0] for r in await cur.fetchall()}
```

- [ ] **Step 5: Run test, verify pass**

Run: `.venv/bin/python -m pytest tests/test_factbase_property_status.py tests/test_factbase_migrations.py -v`
Expected: PASS (migrations test confirms the new step applies cleanly).

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/factbase/schema.py src/open_deep_research/factbase/property_status.py tests/test_factbase_property_status.py
git commit -m "feat(factbase): property_status table + store for confirmed-absent"
```

---

### Task 4: The completeness ledger (pure function)

**Files:**
- Create: `src/open_deep_research/factbase/completeness.py`
- Test: `tests/test_factbase_completeness.py`

**Interfaces:**
- Produces: `assess_property_status(grouped_rows: list[dict], absent: set[str], prof) -> dict[str, str]` returning a status per profile property (`resolved`|`missing_value`|`missing_qualifier`|`missing_narrative`|`confirmed_absent`); `is_complete(status: str, pd) -> bool`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_factbase_completeness.py (new)
from open_deep_research.factbase.profile_schema import profile_from_dict
from open_deep_research.factbase.completeness import assess_property_status, is_complete

PROF = profile_from_dict({"entity_type": "country", "version": "1", "properties": [
    {"name": "scheme", "kind": "name",
     "narrative": {"required": True, "guidance": "g"}, "absence_allowed": False},
    {"name": "cov", "kind": "percentage",
     "identity_qualifiers": ["population_basis"], "required_qualifiers": ["population_basis"]},
]})

def _row(p, value="x", quals=None, narrative="", source_count=2, admission="trusted"):
    return {"property_name": p, "value": value, "qualifiers": quals or {},
            "narrative": narrative, "source_count": source_count, "admission": admission}

def test_resolved_when_value_qualifiers_and_required_narrative_present():
    rows = [_row("scheme", narrative="how it works")]
    st = assess_property_status(rows, set(), PROF)
    assert st["scheme"] == "resolved"

def test_missing_value():
    assert assess_property_status([], set(), PROF)["scheme"] == "missing_value"

def test_missing_required_narrative():
    rows = [_row("scheme", narrative="")]
    assert assess_property_status(rows, set(), PROF)["scheme"] == "missing_narrative"

def test_missing_required_qualifier():
    rows = [_row("cov", quals={})]      # population_basis required, absent
    assert assess_property_status(rows, set(), PROF)["cov"] == "missing_qualifier"

def test_confirmed_absent_from_absent_set():
    st = assess_property_status([], {"cov"}, PROF)
    assert st["cov"] == "confirmed_absent"

def test_is_complete_honours_absence_allowed():
    pd_scheme = PROF.property("scheme")     # absence_allowed False
    pd_cov = PROF.property("cov")           # absence_allowed True (default)
    assert is_complete("resolved", pd_scheme) is True
    assert is_complete("confirmed_absent", pd_scheme) is False   # absence forbidden
    assert is_complete("confirmed_absent", pd_cov) is True
    assert is_complete("missing_value", pd_cov) is False
```

- [ ] **Step 2: Run tests, verify fail**

Run: `.venv/bin/python -m pytest tests/test_factbase_completeness.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `completeness.py`**

```python
"""Per-property completeness ledger for whole-profile facts gathering (pure functions)."""
from __future__ import annotations

# A corroborated provisional counts as resolved (trusted preferred); requiring trusted for
# every property would never terminate. >=2 sources is the provisional bar.
_MIN_PROVISIONAL_SOURCES = 2


def _value_ok(rows) -> bool:
    for r in rows:
        if not str(r.get("value") or "").strip():
            continue
        if r.get("admission") == "trusted":
            return True
        if int(r.get("source_count") or 0) >= _MIN_PROVISIONAL_SOURCES:
            return True
    return False


def assess_property_status(grouped_rows, absent, prof) -> dict:
    by_prop = {}
    for r in grouped_rows:
        by_prop.setdefault(r.get("property_name"), []).append(r)
    out = {}
    for pd in prof.properties:
        p = pd.name
        if p in (absent or set()):
            out[p] = "confirmed_absent"
            continue
        rows = by_prop.get(p) or []
        if not _value_ok(rows):
            out[p] = "missing_value"
            continue
        # qualifiers: the chosen value row must carry every required qualifier
        req = set(getattr(pd, "required_qualifiers", []) or [])
        if req and not any(req <= set((r.get("qualifiers") or {}).keys()) for r in rows):
            out[p] = "missing_qualifier"
            continue
        if getattr(pd, "narrative_required", False) and not any(
                str(r.get("narrative") or "").strip() for r in rows):
            out[p] = "missing_narrative"
            continue
        out[p] = "resolved"
    return out


def is_complete(status: str, pd) -> bool:
    if status == "resolved":
        return True
    if status == "confirmed_absent":
        return bool(getattr(pd, "absence_allowed", True))
    return False
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/python -m pytest tests/test_factbase_completeness.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/completeness.py tests/test_factbase_completeness.py
git commit -m "feat(factbase): per-property completeness ledger (pure)"
```

---

### Task 5: Config — whole-profile mode + round budget

**Files:**
- Modify: `src/open_deep_research/configuration.py`
- Test: `tests/test_model_routing_schema.py` (or the existing Configuration test file; otherwise a new `tests/test_configuration_fields.py`)

**Interfaces:**
- Produces: `Configuration.whole_profile_mode: bool` (default `False`), `Configuration.max_profile_rounds: int` (default `6`).

- [ ] **Step 1: Write failing test**

```python
# tests/test_configuration_fields.py (new)
from open_deep_research.configuration import Configuration

def test_whole_profile_defaults():
    c = Configuration()
    assert c.whole_profile_mode is False
    assert c.max_profile_rounds == 6
```

- [ ] **Step 2: Run test, verify fail**

Run: `.venv/bin/python -m pytest tests/test_configuration_fields.py -v`
Expected: FAIL — fields missing.

- [ ] **Step 3: Add the fields (configuration.py, near `max_fact_rounds`)**

```python
    whole_profile_mode: bool = Field(
        default=False,
        metadata={"x_oap_ui_config": {"type": "boolean", "default": False,
            "description": "Gather EVERY profile property (resolved-or-confirmed-absent) and write a profile-defined subject narrative, instead of only the question-scoped target properties."}})
    max_profile_rounds: int = Field(
        default=6,
        metadata={"x_oap_ui_config": {"type": "number", "default": 6,
            "description": "Hard cap on whole-profile gap rounds (whole_profile_mode). Higher than max_fact_rounds since whole-profile gathering needs more passes."}})
```

- [ ] **Step 4: Run test, verify pass**

Run: `.venv/bin/python -m pytest tests/test_configuration_fields.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/configuration.py tests/test_configuration_fields.py
git commit -m "feat(config): whole_profile_mode + max_profile_rounds"
```

---

### Task 6: `assess_completeness` node + absence judge + loop wiring

**Files:**
- Modify: `src/open_deep_research/deep_researcher.py` (new `judge_absence` helper, new `assess_completeness` node, graph wiring; reuse `_effective_profile_name`, `resolve_in_text`, `_steer_brief_with_catalog`)
- Test: `tests/test_facts_first_mode.py`

**Interfaces:**
- Consumes: `assess_property_status`, `is_complete` (Task 4); `PropertyStatusStore` (Task 3); `CountryResolver.resolve_in_text` (existing).
- Produces: `judge_absence(prop_name, prop_desc, notes_text, model_call) -> bool`; node `assess_completeness` routing `write_research_brief` | `synthesize_narrative`.

- [ ] **Step 1: Write failing tests for `judge_absence` (pure, mocked model)**

```python
# tests/test_facts_first_mode.py (add)
import asyncio as _aio
from open_deep_research import deep_researcher as dr

class _Absent:
    def __init__(self, v): self.absent = v

def test_judge_absence_true_when_model_confirms():
    async def mc(prop, desc, notes): return _Absent(True)
    assert _aio.run(dr.judge_absence("bio", "biometrics", "sources say nothing", mc)) is True

def test_judge_absence_false_and_best_effort_on_error():
    async def yes(prop, desc, notes): return _Absent(False)
    async def boom(prop, desc, notes): raise RuntimeError("x")
    assert _aio.run(dr.judge_absence("bio", "d", "n", yes)) is False
    assert _aio.run(dr.judge_absence("bio", "d", "n", boom)) is False   # error -> not absent (keep trying)
```

- [ ] **Step 2: Run, verify fail**

Run: `.venv/bin/python -m pytest tests/test_facts_first_mode.py -k judge_absence -v`
Expected: FAIL — `judge_absence` undefined.

- [ ] **Step 3: Implement `judge_absence` + `assess_completeness` (deep_researcher.py)**

Add a schema + helper near the other facts helpers:
```python
class AbsenceJudgement(BaseModel):
    """Whether a property is genuinely absent for the subject after a targeted search."""
    absent: bool

async def judge_absence(prop_name, prop_desc, notes_text, model_call) -> bool:
    """True only if the model affirms no data exists for this property after searching.
    Best-effort: any error -> False (treat as still-missing, keep trying within budget)."""
    try:
        res = await model_call(prop_name, prop_desc, notes_text)
        return bool(getattr(res, "absent", False))
    except Exception as e:  # noqa: BLE001
        logger.warning("absence judge failed (non-fatal) for %s: %s", prop_name, e)
        return False
```

Add the node (mirror `assess_sufficiency`, but whole-profile via the ledger). Key points: resolve with `resolve_in_text`; compute the ledger; for still-`missing_value` REQUIRED properties, run `judge_absence` over the round's notes and record confirmed-absent; route to `synthesize_narrative` when every required property `is_complete` or `max_profile_rounds` is hit, else loop with a targeted gap brief.

```python
async def assess_completeness(state: AgentState, config: RunnableConfig) -> Command[Literal["write_research_brief", "synthesize_narrative"]]:
    """Whole-profile: loop until every REQUIRED property is resolved-or-confirmed-absent or budget hit."""
    import aiosqlite
    from open_deep_research.factbase import (entities as fbentities, query as fbquery,
        profile as fbprofile, completeness as fbc, schema as fbschema, migrations as fbmig)
    from open_deep_research.factbase.property_status import PropertyStatusStore
    configurable = Configuration.from_runnable_config(config)
    subject = state.get("subject")
    rounds_used = state.get("fact_rounds_used", 0) or 0
    prof = fbprofile.load(_effective_profile_name(state, configurable))
    ik = fbentities.CountryResolver().resolve_in_text(subject) if subject else None
    if not ik:
        return Command(goto="synthesize_narrative", update={"fact_rounds_used": rounds_used})

    notes_text = "\n".join(state.get("raw_notes", []) or [])[:8000]
    async with aiosqlite.connect(get_db_path(config)) as conn:
        await fbmig.apply(conn, fbschema.STEPS)
        store = PropertyStatusStore(conn)
        absent = await store.absent_properties(ik)
        grouped = await fbquery.FactQuery(conn).show_grouped(ik)
        ledger = fbc.assess_property_status(grouped, absent, prof)
        # affirmative-absence pass for still-missing REQUIRED properties (bounded by the round)
        model_call = _make_absence_judge_call(configurable, config)
        for pd in prof.properties:
            if pd.completeness == "required" and ledger.get(pd.name) == "missing_value" \
                    and pd.absence_allowed and pd.name not in absent:
                if await judge_absence(pd.name, pd.description, notes_text, model_call):
                    await store.record_absent(ik, pd.name, {}, "no data after targeted search", state.get("prealloc_run_id"), None)
                    ledger[pd.name] = "confirmed_absent"
        await conn.commit()

    incomplete = [pd.name for pd in prof.properties
                  if pd.completeness == "required" and not fbc.is_complete(ledger.get(pd.name, "missing_value"), pd)]
    if incomplete and rounds_used + 1 < configurable.max_profile_rounds:
        gap = ("These profile properties are still incomplete and MUST be resolved or, if no data "
               "exists, explicitly confirmed unavailable after searching: " + ", ".join(
                   f"{p} ({ledger.get(p)})" for p in incomplete) + ".")
        return Command(goto="write_research_brief",
                       update={"missing_information": gap, "fact_rounds_used": rounds_used + 1})
    return Command(goto="synthesize_narrative", update={"fact_rounds_used": rounds_used})
```

Add the absence-judge model_call factory (cheap chain, structured output), mirroring `_make_name_consolidation_call`:
```python
def _make_absence_judge_call(configurable, config):
    async def model_call(prop_name, prop_desc, notes_text):
        model = (configurable_model.with_structured_output(AbsenceJudgement)
            .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
            .with_config({"model": configurable.summarization_model,
                "model_chain": configurable.model_chain("summarization"), "stage": "summarization",
                "max_tokens": configurable.summarization_model_max_tokens,
                "api_key": get_api_key_for_model(configurable.summarization_model, config),
                "tags": ["langsmith:nostream"]}))
        prompt = (f"Research notes about a subject are below. For the property '{prop_name}' "
                  f"({prop_desc}), did the research look for it and find that NO data exists / it "
                  f"is not applicable? Set absent=true ONLY if the notes show a genuine, searched "
                  f"absence; set absent=false if it simply wasn't covered yet.\n\nNOTES:\n{notes_text}")
        return await model.ainvoke([HumanMessage(content=prompt)])
    return model_call
```

- [ ] **Step 4: Wire the graph (deep_researcher.py, node/edge registration block ~line 2002+)**

Register the node and route the facts-first loop through it when `whole_profile_mode`:
```python
deep_researcher_builder.add_node("assess_completeness", assess_completeness)
deep_researcher_builder.add_node("synthesize_narrative", synthesize_narrative)   # Task 7
```
At the point where `extract_facts` currently routes to `assess_sufficiency`, branch: if `configurable.whole_profile_mode` route to `assess_completeness`, else keep `assess_sufficiency`. (Find the existing edge from `extract_facts`/`assess_sufficiency`; add a conditional or a small router node. Keep the existing question-scoped path intact for back-compat.)

- [ ] **Step 5: Run the judge_absence tests + existing facts-first suite**

Run: `.venv/bin/python -m pytest tests/test_facts_first_mode.py -v`
Expected: PASS (judge_absence tests green; existing tests unaffected).

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/deep_researcher.py tests/test_facts_first_mode.py
git commit -m "feat(factbase): assess_completeness whole-profile loop + absence judge"
```

---

### Task 7: `synthesize_narrative` node

**Files:**
- Modify: `src/open_deep_research/deep_researcher.py` (`synthesize_narrative` node + `_synthesize_dossier` helper)
- Test: `tests/test_facts_first_mode.py`

**Interfaces:**
- Consumes: grouped facts + `overview_sections` + absent set.
- Produces: `_synthesize_dossier(subject, grouped, absent, overview_sections, model_call) -> str`; node `synthesize_narrative` returning `{"final_report", "messages", "subject"}`.

- [ ] **Step 1: Write failing tests (mocked model + deterministic fallback)**

```python
# tests/test_facts_first_mode.py (add)
def test_synthesize_dossier_uses_model_when_available():
    async def mc(prompt): 
        class R: content = "## How it works\n... ## Coverage\n..."
        return R()
    out = _aio.run(dr._synthesize_dossier("Estonia", [{"property_name":"scheme","value":"eID","variants":["eID"]}],
                                          set(), ["How it works", "Coverage"], mc))
    assert "How it works" in out

def test_synthesize_dossier_falls_back_to_deterministic_on_error():
    async def boom(prompt): raise RuntimeError("x")
    out = _aio.run(dr._synthesize_dossier("Estonia", [{"property_name":"scheme","value":"eID","variants":["eID"]}],
                                          set(), ["Sec"], boom))
    assert "scheme" in out          # deterministic _facts_answer_text fallback rendered
```

- [ ] **Step 2: Run, verify fail**

Run: `.venv/bin/python -m pytest tests/test_facts_first_mode.py -k synthesize_dossier -v`
Expected: FAIL — `_synthesize_dossier` undefined.

- [ ] **Step 3: Implement `_synthesize_dossier` + `synthesize_narrative`**

```python
async def _synthesize_dossier(subject, grouped, absent, overview_sections, model_call) -> str:
    """Profile-defined subject narrative grounded ONLY in gathered facts; deterministic fallback."""
    facts_block = _facts_answer_text(subject, grouped, None)   # readable, raw-value listing
    if not overview_sections:
        return facts_block
    try:
        sections = "\n".join(f"- {s}" for s in overview_sections)
        absent_line = ("Explicitly note these have no data: " + ", ".join(sorted(absent))) if absent else ""
        prompt = (f"Write a concise dossier about {subject}. Cover EACH section below as a '## ' "
                  f"heading, grounded ONLY in the facts provided -- cite nothing not present, and "
                  f"state absences plainly. {absent_line}\n\nSECTIONS:\n{sections}\n\nFACTS:\n{facts_block}")
        resp = await model_call(prompt)
        text = str(getattr(resp, "content", "") or "").strip()
        return text or facts_block
    except Exception as e:  # noqa: BLE001
        logger.warning("narrative synthesis failed; using deterministic facts: %s", e)
        return facts_block

async def synthesize_narrative(state: AgentState, config: RunnableConfig) -> dict:
    import aiosqlite
    from open_deep_research.factbase import (entities as fbentities, query as fbquery,
        profile as fbprofile)
    from open_deep_research.factbase.property_status import PropertyStatusStore
    configurable = Configuration.from_runnable_config(config)
    subject = state.get("subject")
    prof = fbprofile.load(_effective_profile_name(state, configurable))
    ik = fbentities.CountryResolver().resolve_in_text(subject) if subject else None
    grouped, absent = [], set()
    if ik:
        async with aiosqlite.connect(get_db_path(config)) as conn:
            grouped = await fbquery.FactQuery(conn).show_grouped(ik)
            absent = await PropertyStatusStore(conn).absent_properties(ik)
    async def mc(prompt):
        model = configurable_model.with_config({"model": configurable.facts_answer_polish_model or configurable.summarization_model,
            "model_chain": configurable.model_chain("final_report"), "stage": "final_report",
            "max_tokens": configurable.final_report_model_max_tokens,
            "api_key": get_api_key_for_model(configurable.summarization_model, config),
            "tags": ["langsmith:nostream"]})
        return await model.ainvoke([HumanMessage(content=prompt)])
    answer = await _synthesize_dossier(subject, grouped, absent, getattr(prof, "overview_sections", []), mc)
    return {"final_report": answer, "messages": [AIMessage(content=answer)], "subject": subject}
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/python -m pytest tests/test_facts_first_mode.py -k "synthesize or judge_absence" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/deep_researcher.py tests/test_facts_first_mode.py
git commit -m "feat(factbase): synthesize_narrative subject dossier from overview_sections"
```

---

### Task 8: Adopt the new fields in the country profile + full regression

**Files:**
- Modify: `src/open_deep_research/factbase/profiles/country_digital_identity.yaml`
- Test: whole factbase + facts-first suite

- [ ] **Step 1: Add narrative + overview_sections to the profile**

Add per-property `narrative: {required: true, guidance: "..."}` to `foundational_id_scheme`, `id_coverage_pct`, and `legal_basis` (guidance text describing the context each should capture), and a profile-level block:
```yaml
narrative:
  overview_sections:
    - "How the foundational ID scheme works and its legal basis"
    - "Coverage and inclusion gaps"
    - "Governance, privacy, and key risks"
```
Leave `completeness`/`absence_allowed` at defaults except set `id_coverage_pct` and `foundational_id_scheme` to `absence_allowed: false` (a country must have these).

- [ ] **Step 2: Validate the profile + run the suite**

Run: `.venv/bin/python -m pytest tests/ -k "factbase or facts_first or completeness or property_status" -p no:warnings -q`
Expected: PASS. Also run `.venv/bin/python -c "from open_deep_research.factbase import profile; p=profile.load('country_digital_identity'); print(p.overview_sections, p.property('id_coverage_pct').narrative_required)"` → shows the sections + `True`.

- [ ] **Step 3: Commit**

```bash
git add src/open_deep_research/factbase/profiles/country_digital_identity.yaml
git commit -m "feat(profiles): country_digital_identity adopts narrative + overview_sections"
```

- [ ] **Step 4 (verification, not a unit test): live whole-profile probe**

Run a whole-profile Estonia pass against a temp DB and confirm it gathers more properties + writes a sectioned narrative:
`MODEL_ROUTING_PRESET=gemini ODR_PREFLIGHT=warn .venv/bin/python -c "<in-process invoke with configurable whole_profile_mode=True, kb-off, temp db>"`
Expected: the answer is a multi-section dossier; the ledger reaches resolved-or-confirmed-absent for required properties (or budget). Record observations; file follow-ups for absence over/under-claiming.

---

## Self-Review

**Spec coverage:** §1 schema → Task 1; narrative-guidance compilation → Task 2; `property_status`/absence → Task 3; ledger + statuses + trust bar + `absence_allowed` → Task 4; `max_profile_rounds`/mode → Task 5; control flow + confirmed-absent + targeted gap brief + `resolve_in_text` fix → Task 6; `synthesize_narrative` + overview_sections + deterministic fallback → Task 7; profile adoption + back-compat regression + live probe → Task 8. All spec sections mapped.

**Placeholder scan:** No TBDs. Task 6 Step 4 instructs locating the existing `extract_facts`→`assess_sufficiency` edge — that's a concrete wiring instruction, not a placeholder (exact node names given). Task 3 Step 1 says "next version integer" — concrete (read current max in `STEPS`).

**Type consistency:** `PropertyDef.narrative_required/narrative_guidance/completeness/absence_allowed`, `Profile.overview_sections`, `assess_property_status`/`is_complete`, `PropertyStatusStore.record_absent`/`absent_properties`, `judge_absence`, `AbsenceJudgement.absent`, `_synthesize_dossier`, `whole_profile_mode`/`max_profile_rounds` — names used consistently across tasks. The ledger statuses (`resolved`/`missing_value`/`missing_qualifier`/`missing_narrative`/`confirmed_absent`) match between Task 4 and Task 6.

**Ordering:** 1 (schema) → 2 (catalog uses schema) → 3 (table) → 4 (ledger uses schema) → 5 (config) → 6 (node uses 3+4+5) → 7 (narrative) → 8 (adopt + regress).
