# KB-First Research Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Before round-1 research, consult the fact base and skip already-good (trusted + recent + unconflicted) properties — narrowing round-1 `target_properties`, or skipping research entirely when everything's good.

**Architecture:** A read-only gate inside the existing `assess_knowledge` entry node (facts-first / whole-profile modes only). It resolves the run's target properties, applies a conservative reuse predicate over `FactQuery.show_grouped(instance_key)`, and either routes to `answer_from_facts` (all good) or to `write_research_brief` with a narrowed `target_properties`. No new node, no schema change, no persistence change.

**Tech Stack:** Python 3, LangGraph (Command-goto routing), `aiosqlite`. Tests: `asyncio.run(...)` + `monkeypatch` driving single nodes via `import open_deep_research.deep_researcher as dr` (house style, see `tests/test_assess_sufficiency.py`).

## Global Constraints

- **Conservative reuse predicate:** a property is reusable iff its grouped value is **not `in_conflict`** AND has a **`trusted_captured_at`** (newest `created_at` among the group's `admission='trusted'` rows) **within `kb_reuse_max_age_days`** (default 180). `trusted_captured_at` non-None already implies a trusted row exists. No trusted row / unparseable timestamp → not reusable (re-research).
- **Scope: facts-first + whole-profile modes only.** Prose mode keeps its existing LLM dossier cache-hit (the `else` path in `assess_knowledge`) untouched. Gated by `Configuration.kb_first_gate` (default `False` → today's behavior) AND `use_knowledge_base`.
- **The gate-skip is a cache answer:** when all target props are reusable, route `Command(goto="answer_from_facts", update={"subject": subject, "answered_from_cache": True, "target_properties": <reusable>})`. `persist_research` ALREADY exempts a run with `answered_from_cache` + `subject` from the empty-run error gate (`persistence.py:168-174`, the first branch) — **do not modify persistence**.
- **Narrowing needs no new flag:** `write_research_brief` round-1 already keeps a pre-set `target_properties` (`brief.py:320`, the `not target_properties` guard). The gate sets `target_properties = <to_research>`; do NOT add a `kb_prefiltered` field.
- **`answer_from_facts` reachability:** widen `assess_knowledge`'s return annotation to `Command[Literal["answer_from_dossier", "write_research_brief", "clarify_with_user", "answer_from_facts"]]`. The node is already registered (`deep_researcher.py:192`); no `add_node`/edge change (it has a static out-edge to `persist_research`).
- **Best-effort:** any failure in the gate (subject unresolved, KB read error, predicate error) falls through to the normal research path — never block, never skip-on-error.
- **Out of scope:** per-property freshness windows; transitioning `lifecycle`; prose-mode changes; ④.

---

### Task 1: Surface `trusted_captured_at` on grouped facts

**Files:**
- Modify: `src/open_deep_research/factbase/query.py` (`_rows` SELECT; `group_by_canonical`)
- Test: `tests/test_query_trusted_captured_at.py`

**Interfaces:**
- Produces: each `show_grouped(instance_key)` row gains `trusted_captured_at: str | None` = `MAX(created_at)` over the group's `admission='trusted'` rows (None if no trusted row).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_query_trusted_captured_at.py
import asyncio, aiosqlite
from open_deep_research.factbase import schema, migrations, query


async def _seed(conn):
    await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, thread_id TEXT);")
    await conn.commit()
    await migrations.apply(conn, schema.STEPS)
    # two facts for the same (instance, property, canonical value): one trusted (older),
    # one provisional (newer). trusted_captured_at must be the TRUSTED row's created_at.
    await conn.execute("INSERT INTO fact (instance_key, property_name, tuple_key, value, canonical_value, "
                       "admission, lifecycle, as_of, created_at) "
                       "VALUES ('EST','legal_basis','EST|legal_basis','Act X','Act X','trusted','current',2020,'2026-01-01T00:00:00Z')")
    await conn.execute("INSERT INTO fact (instance_key, property_name, tuple_key, value, canonical_value, "
                       "admission, lifecycle, as_of, created_at) "
                       "VALUES ('EST','legal_basis','EST|legal_basis','Act X','Act X','provisional','current',2020,'2026-06-01T00:00:00Z')")
    await conn.commit()


def test_trusted_captured_at_is_max_over_trusted_rows():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            grouped = await query.FactQuery(conn).show_grouped("EST")
            row = next(g for g in grouped if g["property_name"] == "legal_basis")
            # admission is "trusted" (any-trusted), and trusted_captured_at is the TRUSTED row's ts
            assert row["admission"] == "trusted"
            assert row["trusted_captured_at"] == "2026-01-01T00:00:00Z"
    asyncio.run(run())


def test_trusted_captured_at_none_when_no_trusted_row():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, thread_id TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            await conn.execute("INSERT INTO fact (instance_key, property_name, tuple_key, value, canonical_value, "
                               "admission, lifecycle, as_of, created_at) "
                               "VALUES ('EST','x','EST|x','v','v','provisional','current',2020,'2026-06-01T00:00:00Z')")
            await conn.commit()
            grouped = await query.FactQuery(conn).show_grouped("EST")
            assert grouped[0]["trusted_captured_at"] is None
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src /mnt/c/Users/abradley/Projects/IdentityInnovation/search/open_deep_research/.venv/bin/python -m pytest tests/test_query_trusted_captured_at.py -v`
Expected: FAIL — `KeyError: 'trusted_captured_at'`.

- [ ] **Step 3: Add `created_at` to the SELECT and aggregate `trusted_captured_at`**

In `src/open_deep_research/factbase/query.py`, add `f.created_at` to the `_rows` SELECT (after `f.lifecycle`):
```python
            "f.unit, f.canonical_value, f.canonical_unit, f.narrative, f.admission, f.lifecycle, "
            "f.created_at, "
```
In `group_by_canonical`, seed the trusted-capture tracker and aggregate it. In the group-init dict add `"trusted_captured_at": None` and `g["_trusted_created"] = []`; in the per-row loop, after the `admission` handling, add:
```python
        if r.get("admission") == "trusted" and r.get("created_at"):
            g["_trusted_created"].append(r["created_at"])
```
In the finalize loop (where `source_count`/`variants`/`narrative` are popped), add:
```python
        tc = g.pop("_trusted_created")
        g["trusted_captured_at"] = max(tc) if tc else None
```

- [ ] **Step 4: Run test to verify it passes**

Run the Step 2 command. Expected: PASS (both tests).

- [ ] **Step 5: Run query/completeness regression**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/ -k "query or completeness or factbase" -q`
Expected: PASS (the new key is additive; existing consumers ignore it).

- [ ] **Step 6: Commit**

```bash
git add tests/test_query_trusted_captured_at.py src/open_deep_research/factbase/query.py
git commit -m "feat(factbase): surface trusted_captured_at on grouped facts"
```

---

### Task 2: The reuse predicate

**Files:**
- Create: `src/open_deep_research/factbase/reuse.py`
- Test: `tests/test_reuse_predicate.py`

**Interfaces:**
- Consumes: a grouped fact row (`trusted_captured_at`, `in_conflict`) from Task 1.
- Produces: `is_reusable(group_row: dict, *, now: datetime, max_age_days: int) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reuse_predicate.py
from datetime import datetime, timezone
from open_deep_research.factbase.reuse import is_reusable

NOW = datetime(2026, 6, 30, tzinfo=timezone.utc)


def _row(**kw):
    base = {"in_conflict": False, "trusted_captured_at": "2026-06-01T00:00:00Z"}
    base.update(kw); return base


def test_trusted_recent_unconflicted_is_reusable():
    assert is_reusable(_row(), now=NOW, max_age_days=180) is True

def test_no_trusted_row_not_reusable():
    assert is_reusable(_row(trusted_captured_at=None), now=NOW, max_age_days=180) is False

def test_trusted_but_stale_not_reusable():
    assert is_reusable(_row(trusted_captured_at="2024-01-01T00:00:00Z"), now=NOW, max_age_days=180) is False

def test_in_conflict_not_reusable():
    assert is_reusable(_row(in_conflict=True), now=NOW, max_age_days=180) is False

def test_unparseable_timestamp_not_reusable():
    assert is_reusable(_row(trusted_captured_at="not-a-date"), now=NOW, max_age_days=180) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/test_reuse_predicate.py -v`
Expected: FAIL — `ModuleNotFoundError: ...factbase.reuse`.

- [ ] **Step 3: Implement `reuse.py`**

```python
# src/open_deep_research/factbase/reuse.py
"""KB-first reuse predicate: is a property's current value good enough to skip researching?

Conservative: a trusted (admission), unconflicted value captured within a freshness window.
Trust and recency are evaluated on the SAME rows via `trusted_captured_at` (the newest capture
among the group's trusted rows), so a stale trusted row + a fresh provisional row can't qualify.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def is_reusable(group_row: dict, *, now: datetime, max_age_days: int) -> bool:
    """True iff the property's trusted value is unconflicted and captured within the window."""
    if group_row.get("in_conflict"):
        return False
    captured = _parse(group_row.get("trusted_captured_at"))
    if captured is None:
        return False
    return (now - captured).days <= max_age_days
```

- [ ] **Step 4: Run test to verify it passes**

Run the Step 2 command. Expected: PASS (all five).

- [ ] **Step 5: Commit**

```bash
git add tests/test_reuse_predicate.py src/open_deep_research/factbase/reuse.py
git commit -m "feat(factbase): conservative KB reuse predicate (trusted + recent + unconflicted)"
```

---

### Task 3: Configuration flags

**Files:**
- Modify: `src/open_deep_research/configuration.py` (add two fields near `use_knowledge_base`)
- Test: `tests/test_kb_gate_config.py`

**Interfaces:**
- Produces: `Configuration.kb_first_gate: bool = False`, `Configuration.kb_reuse_max_age_days: int = 180`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_kb_gate_config.py
from open_deep_research.configuration import Configuration


def test_kb_gate_defaults():
    c = Configuration()
    assert c.kb_first_gate is False
    assert c.kb_reuse_max_age_days == 180

def test_kb_gate_from_config():
    c = Configuration.from_runnable_config(
        {"configurable": {"kb_first_gate": True, "kb_reuse_max_age_days": 30}})
    assert c.kb_first_gate is True and c.kb_reuse_max_age_days == 30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/test_kb_gate_config.py -v`
Expected: FAIL — `AttributeError: ... 'kb_first_gate'`.

- [ ] **Step 3: Add the fields**

In `src/open_deep_research/configuration.py`, after the `use_knowledge_base` field:
```python
    kb_first_gate: bool = Field(
        default=False,
        metadata={"x_oap_ui_config": {"type": "boolean", "default": False,
            "description": "Facts-first / whole-profile only: before round-1 research, skip properties whose stored value is already trusted, unconflicted, and captured within kb_reuse_max_age_days. If all target properties are good, answer from the fact base without researching."}}
    )
    kb_reuse_max_age_days: int = Field(
        default=180,
        metadata={"x_oap_ui_config": {"type": "number", "default": 180, "min": 1, "max": 3650,
            "description": "KB-first gate: a trusted fact captured within this many days is reused (research skipped); older than this it is re-verified."}}
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_kb_gate_config.py src/open_deep_research/configuration.py
git commit -m "feat(config): kb_first_gate + kb_reuse_max_age_days"
```

---

### Task 4: Shared target-property resolver

**Files:**
- Modify: `src/open_deep_research/nodes/profiles.py` (add `resolve_run_target_properties`)
- Modify: `src/open_deep_research/nodes/brief.py` (`write_research_brief` uses the helper)
- Test: `tests/test_resolve_run_target_properties.py`

**Interfaces:**
- Produces: `async def resolve_run_target_properties(question, profile_name, configurable, config) -> list[str]` — whole-profile → all property names; facts-first → `resolve_target_properties(...)`.
- `write_research_brief` round-1 resolves `target_properties` via this helper (behavior-preserving).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_resolve_run_target_properties.py
import asyncio
import open_deep_research.nodes.profiles as profiles


def test_whole_profile_returns_all(monkeypatch):
    prof = type("P", (), {"properties": [type("X", (), {"name": "a", "value_kind": "str"})(),
                                          type("X", (), {"name": "b", "value_kind": "str"})()]})()
    monkeypatch.setattr("open_deep_research.factbase.profile.load", lambda n: prof)
    cfg = type("C", (), {"whole_profile_mode": True, "facts_first_mode": False})()
    out = asyncio.run(profiles.resolve_run_target_properties("q", "country_digital_identity", cfg, {}))
    assert out == ["a", "b"]


def test_facts_first_delegates(monkeypatch):
    prof = type("P", (), {"properties": [type("X", (), {"name": "a", "value_kind": "str"})()]})()
    monkeypatch.setattr("open_deep_research.factbase.profile.load", lambda n: prof)
    async def fake_resolve(question, p, c, cfg): return ["a"]
    monkeypatch.setattr(profiles, "resolve_target_properties", fake_resolve)
    cfg = type("C", (), {"whole_profile_mode": False, "facts_first_mode": True})()
    out = asyncio.run(profiles.resolve_run_target_properties("q", "p", cfg, {}))
    assert out == ["a"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/test_resolve_run_target_properties.py -v`
Expected: FAIL — `AttributeError: ... 'resolve_run_target_properties'`.

- [ ] **Step 3: Add the helper**

In `src/open_deep_research/nodes/profiles.py`, add:
```python
async def resolve_run_target_properties(question, profile_name, configurable, config) -> list[str]:
    """The run's target properties: whole-profile = all props; facts-first = question-scoped."""
    from open_deep_research.factbase import profile as _fbprofile
    prof = _fbprofile.load(profile_name)
    if configurable.whole_profile_mode:
        return [pd.name for pd in prof.properties]
    return await resolve_target_properties(question, prof, configurable, config)
```

- [ ] **Step 4: Refactor `write_research_brief` to use it**

In `src/open_deep_research/nodes/brief.py`, replace the round-1 resolution block (`brief.py:320-338`) so the `not target_properties` branch calls the helper (behavior-preserving — whole-profile still all props, facts-first still `resolve_target_properties`):
```python
    target_properties = state.get("target_properties")
    if configurable.facts_first_mode or configurable.whole_profile_mode:
        from open_deep_research.factbase import profile as _fbprofile
        from open_deep_research.nodes.profiles import resolve_run_target_properties
        _prof = _fbprofile.load(profile_name)
        if not target_properties:
            target_properties = await resolve_run_target_properties(
                question, profile_name, configurable, config)
        if target_properties:
            research_brief = _steer_brief_with_catalog(research_brief, _prof, target_properties)
```

- [ ] **Step 5: Run test + brief regression**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/test_resolve_run_target_properties.py -q && PYTHONPATH=src …/.venv/bin/python -m pytest tests/ -k "brief or write_research or target_propert" -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_resolve_run_target_properties.py src/open_deep_research/nodes/profiles.py src/open_deep_research/nodes/brief.py
git commit -m "refactor(brief): extract resolve_run_target_properties (shared by the KB gate)"
```

---

### Task 5: The KB-first gate in `assess_knowledge`

**Files:**
- Modify: `src/open_deep_research/nodes/brief.py` (`assess_knowledge`: widen `Literal`, add the fact-gate branch)
- Test: `tests/test_kb_first_gate.py`

**Interfaces:**
- Consumes: Task 1 (`trusted_captured_at`), Task 2 (`is_reusable`), Task 3 (config), Task 4 (`resolve_run_target_properties`).
- Produces: in facts-first/whole-profile mode with `kb_first_gate` on, `assess_knowledge` routes to `answer_from_facts` (all reusable; sets `answered_from_cache`+`target_properties`) or `write_research_brief` (partial; narrowed `target_properties`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_kb_first_gate.py
import asyncio
import open_deep_research.deep_researcher as dr
import open_deep_research.nodes.brief as brief
import open_deep_research.factbase.query as q
import open_deep_research.factbase.entities as ent


def _wire(monkeypatch, *, grouped, subject="Estonia"):
    monkeypatch.setattr(brief, "get_subject_names", _aval([subject]))
    monkeypatch.setattr(brief, "_resolve_subject", _aval(subject))
    monkeypatch.setattr(brief, "get_subject_by_slug", _aval({"current_report": "old dossier"}))
    monkeypatch.setattr(ent.CountryResolver, "resolve_in_text", lambda self, s: "EST")
    monkeypatch.setattr(ent.CountryResolver, "resolve", lambda self, s: "EST")
    async def fake_grouped(self, key): return grouped
    monkeypatch.setattr(q.FactQuery, "show_grouped", fake_grouped)
    monkeypatch.setattr(brief, "resolve_run_target_properties", _aval(["a", "b"]))


def _aval(v):
    async def f(*a, **k): return v
    return f


def _cfg(**kw):
    base = {"use_knowledge_base": True, "kb_first_gate": True, "whole_profile_mode": True,
            "facts_first_mode": False, "allow_clarification": False, "kb_reuse_max_age_days": 180,
            "database_path": "/tmp/kbgate.db"}
    base.update(kw); return {"configurable": base}


def _good(name): return {"property_name": name, "in_conflict": False, "trusted_captured_at": "2026-06-01T00:00:00Z"}
def _bad(name): return {"property_name": name, "in_conflict": False, "trusted_captured_at": None}


def test_all_reusable_routes_to_answer_from_facts(monkeypatch):
    _wire(monkeypatch, grouped=[_good("a"), _good("b")])
    cmd = asyncio.run(brief.assess_knowledge({"messages": []}, _cfg()))
    assert cmd.goto == "answer_from_facts"
    assert cmd.update.get("answered_from_cache") is True
    assert set(cmd.update.get("target_properties")) == {"a", "b"}


def test_partial_narrows_target_properties(monkeypatch):
    _wire(monkeypatch, grouped=[_good("a"), _bad("b")])
    cmd = asyncio.run(brief.assess_knowledge({"messages": []}, _cfg()))
    assert cmd.goto == "write_research_brief"
    assert cmd.update.get("target_properties") == ["b"]


def test_gate_off_uses_existing_flow(monkeypatch):
    _wire(monkeypatch, grouped=[_good("a"), _good("b")])
    # kb_first_gate off -> the prose LLM-assessment path; stub it to a known route
    monkeypatch.setattr(brief, "configurable_model", brief.configurable_model)
    cmd = asyncio.run(brief.assess_knowledge({"messages": []}, _cfg(kb_first_gate=False,
                                              whole_profile_mode=False, facts_first_mode=False)))
    assert cmd.goto in ("answer_from_dossier", "write_research_brief", "clarify_with_user")


def test_kb_read_error_falls_through(monkeypatch):
    _wire(monkeypatch, grouped=[])
    async def boom(self, key): raise RuntimeError("db locked")
    monkeypatch.setattr(q.FactQuery, "show_grouped", boom)
    cmd = asyncio.run(brief.assess_knowledge({"messages": []}, _cfg()))
    assert cmd.goto == "write_research_brief"   # falls through to normal research
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/test_kb_first_gate.py -v`
Expected: FAIL — the gate branch doesn't exist; routes don't match.

- [ ] **Step 3: Widen the annotation + add the gate branch**

In `src/open_deep_research/nodes/brief.py`, change `assess_knowledge`'s signature return annotation to include `answer_from_facts`:
```python
async def assess_knowledge(state: AgentState, config: RunnableConfig) -> Command[Literal["answer_from_dossier", "write_research_brief", "clarify_with_user", "answer_from_facts"]]:
```
Then insert the fact-gate branch **after** subject resolution and **before** the prose-dossier Step 3 (i.e., after `existing = ...; dossier = ...`), so it only runs in facts/profile mode with the flag on:
```python
    # KB-first gate (facts-first / whole-profile): skip already-good properties before round 1.
    if configurable.kb_first_gate and (configurable.facts_first_mode or configurable.whole_profile_mode):
        try:
            from open_deep_research.factbase.reuse import is_reusable
            from open_deep_research.factbase.query import FactQuery
            from open_deep_research.factbase.entities import CountryResolver
            from open_deep_research.nodes.profiles import resolve_run_target_properties
            from datetime import datetime, timezone
            profile_name = configurable.profile_name  # selected profile (config default)
            targets = await resolve_run_target_properties(question, profile_name, configurable, config)
            ik = CountryResolver().resolve_in_text(subject) or CountryResolver().resolve(subject)
            now = datetime.now(timezone.utc)
            reusable = []
            if ik:
                async with aiosqlite.connect(db_path) as _conn:
                    grouped = await FactQuery(_conn).show_grouped(ik)
                by_prop = {g.get("property_name"): g for g in grouped}
                reusable = [p for p in targets
                            if p in by_prop and is_reusable(by_prop[p], now=now,
                                                            max_age_days=configurable.kb_reuse_max_age_days)]
            to_research = [p for p in targets if p not in reusable]
            if targets and not to_research:
                return Command(goto="answer_from_facts",
                               update={"subject": subject, "answered_from_cache": True,
                                       "target_properties": reusable})
            if reusable:  # partial: research only the delta
                gap = ("These properties are already known and trusted (skipped): "
                       + ", ".join(reusable) + ". Research only: " + ", ".join(to_research) + ".")
                return Command(goto="write_research_brief",
                               update={"subject": subject, "target_properties": to_research,
                                       "missing_information": gap})
        except Exception as e:
            logger.warning("KB-first gate failed; researching normally: %s", e)
        # nothing reusable (or error) -> fall through to the normal flow below
```
(`aiosqlite` and `logger` are already imported in `brief.py`; `db_path`, `question`, `subject`, `configurable` are already in scope at this point.) The existing prose Step 3 (`if not dossier: ...` and the `KnowledgeAssessment` flow) remains the fallback for prose mode and for the no-reusable case.

> Note: `profile_name` here uses `configurable.profile_name` (the configured active profile); the per-question `selected_profile_name` is set later in `write_research_brief`. For the gate's reuse check this is the correct profile to target.

- [ ] **Step 4: Run test to verify it passes**

Run the Step 2 command. Expected: PASS (all four).

- [ ] **Step 5: Verify the graph still compiles with the widened route**

Run: `PYTHONPATH=src …/.venv/bin/python -c "import open_deep_research.deep_researcher as dr; dr.deep_researcher; print('graph ok')"`
Expected: prints `graph ok` (LangGraph accepts `answer_from_facts` as a Command-goto target from `assess_knowledge`).

- [ ] **Step 6: Broad regression**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/ -k "brief or knowledge or assess or graph or query or completeness or persist" -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add tests/test_kb_first_gate.py src/open_deep_research/nodes/brief.py
git commit -m "feat(graph): KB-first gate in assess_knowledge (skip already-good properties)"
```

---

## Self-Review

**Spec coverage:** predicate (Task 2 = `is_reusable` over `trusted_captured_at`); `trusted_captured_at` surfaced (Task 1); config flags (Task 3); shared resolver + the gate (Tasks 4-5); all-good → `answer_from_facts` with `answered_from_cache` (Task 5); partial → narrowed `target_properties` (Task 5); best-effort fallback (Task 5); facts-first + whole-profile only, prose untouched (Task 5 branch condition). Persistence needs no change (Global Constraints — it already exempts `answered_from_cache`).

**Placeholder scan:** none — every step has full code/commands (`…/.venv/bin/python` abbreviates the verified interpreter path `/mnt/c/Users/abradley/Projects/IdentityInnovation/search/open_deep_research/.venv/bin/python`).

**Type consistency:** `trusted_captured_at` (Task 1) is read by `is_reusable` (Task 2) and the gate (Task 5); `resolve_run_target_properties(question, profile_name, configurable, config)` (Task 4) is called by the gate and `write_research_brief` (Task 4) identically; `kb_first_gate`/`kb_reuse_max_age_days` (Task 3) are read in Task 5; the gate's `Command` targets are all in the widened `Literal`.

**Refinements vs spec (simpler than written, flagged for the reviewer):** the spec's component 6 implied a `persist_research` change — not needed (it already exempts `answered_from_cache`+`subject`); the spec mentioned a `kb_prefiltered` flag — not needed (`write_research_brief` already honors a pre-set `target_properties`). Both are documented in Global Constraints.
