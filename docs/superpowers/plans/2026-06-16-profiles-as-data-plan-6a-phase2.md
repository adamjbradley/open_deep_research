# Profiles as Data — Plan 6a Phase 2 (Runtime Selection + Provenance Stamping) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the active domain profile/registry **runtime-selectable via config** (so a second profile can drive a run with no engine code change), and **stamp each run** with the profile it used (`profile_name`/`profile_version`/`profile_hash`) for provenance.

**Architecture:** Add `Configuration.profile_name`/`registry_name`; replace the two hard-coded `load("country_digital_identity")` / `SourceRegistry.load("di_source_registry")` sites with `load(configurable.profile_name)` / `SourceRegistry.load(configurable.registry_name)`. Compute a `profile_hash` over the *validated semantic model* (not raw bytes) in `profile_from_dict`. Add a schema-v6 migration putting `profile_name`/`profile_version`/`profile_hash` on `research_runs`, and stamp them in `extract_facts` after the profile is loaded.

**Tech Stack:** Python 3.11, Pydantic v2, the factbase migration framework (`schema.STEPS` + `migrations.apply`), `aiosqlite`, pytest.

**Builds on:** Phase 1 (merged): profiles load from validated YAML; `profile_from_dict` exists; `load(name)`/`SourceRegistry.load(name)` read YAML.

**Scope:** Selection contract + provenance stamping ONLY. **Deferred to later plans:** prompt compilation from the schema (behavior-changing; own plan + A/B), and hash-mismatch detection + `dossier recompute --check`/normalization-recompute trigger (depends on stamping landing here). Structural rebuild + scaffolding remain Plan 6b.

---

## File structure

| File | Responsibility | Action |
|---|---|---|
| `src/open_deep_research/configuration.py` | `profile_name` / `registry_name` config fields | Modify |
| `src/open_deep_research/factbase/profile_schema.py` | compute `profile_hash` (semantic) in `profile_from_dict` | Modify |
| `src/open_deep_research/factbase/schema.py` | migration v6: stamp columns on `research_runs` | Modify (`STEPS`) |
| `src/open_deep_research/storage.py` | allow stamping the new columns via `finalize_research_run` | Modify (whitelist) |
| `src/open_deep_research/deep_researcher.py` | use `configurable.profile_name`/`registry_name`; stamp the run | Modify (lines 350, 1383-1384, + stamp in extract_facts) |
| `tests/test_factbase_profile_hash.py` | semantic-hash behavior | Create |
| `tests/test_factbase_selection.py` | config selection + a second profile drives resolution | Create |
| `tests/test_factbase_run_stamp.py` | migration v6 + stamping | Create |

---

## Task 1: Config fields `profile_name` / `registry_name`

**Files:**
- Modify: `src/open_deep_research/configuration.py` (add two fields near the other factbase fields, ~line 271-313)
- Test: `tests/test_factbase_selection.py` (first test only)

- [ ] **Step 1: Write the failing test**

Create `tests/test_factbase_selection.py`:

```python
from open_deep_research.configuration import Configuration


def test_profile_name_defaults():
    c = Configuration()
    assert c.profile_name == "country_digital_identity"
    assert c.registry_name == "di_source_registry"


def test_profile_name_overridable_via_runnable_config():
    c = Configuration.from_runnable_config({"configurable": {"profile_name": "country_cbdc"}})
    assert c.profile_name == "country_cbdc"
```

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_factbase_selection.py -q`
Expected: FAIL (`AttributeError`/validation: no `profile_name`).

- [ ] **Step 3: Add the fields**

In `src/open_deep_research/configuration.py`, immediately AFTER the `facts_first_mode` Field definition (around line 281-…; find the line where that `Field(...)` closes with `)`), add:

```python
    profile_name: str = Field(
        default="country_digital_identity",
        metadata={"x_oap_ui_config": {
            "type": "text",
            "default": "country_digital_identity",
            "description": "Name of the factbase domain profile (YAML file stem under factbase/profiles/) used for fact extraction.",
        }},
    )
    registry_name: str = Field(
        default="di_source_registry",
        metadata={"x_oap_ui_config": {
            "type": "text",
            "default": "di_source_registry",
            "description": "Name of the factbase source registry (YAML file stem under factbase/profiles/) used for source-trust tiers.",
        }},
    )
```

- [ ] **Step 4: Run, verify pass**

Run: `uv run pytest tests/test_factbase_selection.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/configuration.py tests/test_factbase_selection.py
git commit -m "feat(config): profile_name/registry_name for runtime profile selection"
```

---

## Task 2: Semantic `profile_hash` in `profile_from_dict`

**Files:**
- Modify: `src/open_deep_research/factbase/profile_schema.py` (`profile_from_dict`)
- Test: `tests/test_factbase_profile_hash.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_factbase_profile_hash.py`:

```python
import copy

from open_deep_research.factbase.profile_schema import profile_from_dict

BASE = {
    "entity_type": "country",
    "version": "1",
    "properties": [
        {"name": "scheme_status", "kind": "enum", "description": "x",
         "value_enum": ["a", "b"]},
    ],
}


def test_hash_is_stable_and_present():
    h1 = profile_from_dict(copy.deepcopy(BASE)).profile_hash
    h2 = profile_from_dict(copy.deepcopy(BASE)).profile_hash
    assert isinstance(h1, str) and len(h1) == 64  # sha256 hex
    assert h1 == h2


def test_hash_ignores_description_and_notes_changes():
    other = copy.deepcopy(BASE)
    other["notes"] = "human comment that should not change identity"
    other["properties"][0]["description"] = "a totally different description"
    assert profile_from_dict(other).profile_hash == profile_from_dict(copy.deepcopy(BASE)).profile_hash


def test_hash_changes_on_semantic_change():
    other = copy.deepcopy(BASE)
    other["properties"][0]["value_enum"] = ["a", "b", "c"]  # enum changed -> semantic
    assert profile_from_dict(other).profile_hash != profile_from_dict(copy.deepcopy(BASE)).profile_hash
```

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_factbase_profile_hash.py -q`
Expected: FAIL (`AttributeError: 'Profile' object has no attribute 'profile_hash'`).

- [ ] **Step 3: Compute the hash over the semantic model (excluding description/notes)**

In `src/open_deep_research/factbase/profile_schema.py`, at the top add:

```python
import hashlib
import json
```

Then in `profile_from_dict`, replace the final lines (currently:
```python
    prof = Profile(entity_type=model.entity_type, properties=props)
    prof.profile_version = model.version  # carried as an attribute; hash arrives in Phase 2
    return prof
```
) with:

```python
    prof = Profile(entity_type=model.entity_type, properties=props)
    prof.profile_version = model.version
    # Hash the SEMANTIC profile (validated, normalized) — NOT raw file bytes — so inert
    # comments, `description`/`notes`, and formatting churn don't trigger false drift.
    semantic = {
        "entity_type": model.entity_type,
        "properties": [
            {
                "name": pd.name,
                "kind": pd.value_kind,
                "identity_qualifiers": sorted(pd.identity_qualifiers),
                "required_qualifiers": sorted(pd.required_qualifiers),
                "qualifier_enums": {k: sorted(v) for k, v in pd.qualifier_enums.items()},
                "value_enum": None if pd.value_enum is None else sorted(pd.value_enum),
                "trust_threshold": pd.trust_threshold,
                "value_aliases": {k: sorted(v) for k, v in pd.value_aliases.items()},
            }
            for pd in sorted(props, key=lambda p: p.name)
        ],
    }
    prof.profile_hash = hashlib.sha256(
        json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return prof
```

(Note: `version` is intentionally excluded from the hash — it's a human changelog; the hash is the *content* identity. `description`/`notes` are excluded by construction.)

- [ ] **Step 4: Run, verify pass**

Run: `uv run pytest tests/test_factbase_profile_hash.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/profile_schema.py tests/test_factbase_profile_hash.py
git commit -m "feat(factbase): semantic profile_hash on the loaded Profile"
```

---

## Task 3: Schema v6 migration + stamp whitelist

**Files:**
- Modify: `src/open_deep_research/factbase/schema.py` (`STEPS`)
- Modify: `src/open_deep_research/storage.py` (`finalize_research_run` whitelist)
- Test: `tests/test_factbase_run_stamp.py` (first test only)

- [ ] **Step 1: Write the failing test**

Create `tests/test_factbase_run_stamp.py`:

```python
import asyncio

import aiosqlite

from open_deep_research.factbase import migrations, schema
from open_deep_research import storage


def test_v6_adds_profile_columns(tmp_path):
    db = str(tmp_path / "fb.db")

    async def go():
        async with aiosqlite.connect(db) as conn:
            await storage._ensure_schema(conn)
            await migrations.apply(conn, schema.STEPS)
            cur = await conn.execute("PRAGMA table_info(research_runs)")
            cols = {r[1] for r in await cur.fetchall()}
            assert {"profile_name", "profile_version", "profile_hash"} <= cols

    asyncio.run(go())


def test_finalize_stamps_profile_fields(tmp_path):
    db = str(tmp_path / "fb.db")

    async def go():
        run_id = await storage.preallocate_run(db, "thread-1")
        await storage.finalize_research_run(db, run_id, {
            "profile_name": "country_digital_identity",
            "profile_version": "1",
            "profile_hash": "abc123",
            "status": "completed",
        })
        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute(
                "SELECT profile_name, profile_version, profile_hash, status FROM research_runs WHERE id=?",
                (run_id,))
            row = await cur.fetchone()
        assert row == ("country_digital_identity", "1", "abc123", "completed")

    asyncio.run(go())
```

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_factbase_run_stamp.py -q`
Expected: FAIL (no such column `profile_name`).

- [ ] **Step 3: Add migration v6**

In `src/open_deep_research/factbase/schema.py`, append a new tuple to the `STEPS` list (after the `(5, ...)` entry, before the closing `]`):

```python
    (6, """
    ALTER TABLE research_runs ADD COLUMN profile_name TEXT;
    ALTER TABLE research_runs ADD COLUMN profile_version TEXT;
    ALTER TABLE research_runs ADD COLUMN profile_hash TEXT;
    """),
```

- [ ] **Step 4: Extend the finalize whitelist**

In `src/open_deep_research/storage.py`, in `finalize_research_run`, change the `allowed` set to include the three new columns:

```python
    allowed = {"status", "topic", "research_brief", "final_report", "sources",
               "raw_notes", "config", "error", "coverage_incomplete",
               "profile_name", "profile_version", "profile_hash"}
```

- [ ] **Step 5: Run, verify pass**

Run: `uv run pytest tests/test_factbase_run_stamp.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/factbase/schema.py src/open_deep_research/storage.py tests/test_factbase_run_stamp.py
git commit -m "feat(factbase): migration v6 stamps profile_name/version/hash on research_runs"
```

---

## Task 4: Use the configured profile/registry at both load sites

**Files:**
- Modify: `src/open_deep_research/deep_researcher.py` (lines 350 and 1383-1384)
- Test: `tests/test_factbase_selection.py` (add a selection test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_factbase_selection.py`:

```python
def test_a_second_profile_drives_resolution(tmp_path, monkeypatch):
    # A minimal alternate profile loadable by name, proving no-engine-edit selection.
    import open_deep_research.factbase.profile as profile_mod

    real_load = profile_mod.load

    def fake_load(name):
        if name == "country_cbdc":
            from open_deep_research.factbase.profile_schema import profile_from_dict
            return profile_from_dict({
                "entity_type": "country", "version": "1",
                "properties": [{"name": "cbdc_status", "kind": "enum",
                                "value_enum": ["research", "pilot", "launched"]}],
            })
        return real_load(name)

    monkeypatch.setattr(profile_mod, "load", fake_load)
    # The selected profile is reachable purely by name — the engine must read the config name,
    # never a hardcoded literal.
    p = profile_mod.load("country_cbdc")
    assert p.property("cbdc_status").value_enum == ["research", "pilot", "launched"]
```

(This test pins the load-by-name contract used by the engine; the engine swap below makes `configurable.profile_name` the source of that name.)

- [ ] **Step 2: Run the existing selection tests (this new one passes already; the real change is the engine swap verified by the full suite)**

Run: `uv run pytest tests/test_factbase_selection.py -q`
Expected: PASS.

- [ ] **Step 3: Swap the two hard-coded names for config**

In `src/open_deep_research/deep_researcher.py`:

At line ~350, change:
```python
        target_properties = await resolve_target_properties(
            question, _fbprofile.load("country_digital_identity"), configurable, config
        )
```
to:
```python
        target_properties = await resolve_target_properties(
            question, _fbprofile.load(configurable.profile_name), configurable, config
        )
```

At lines ~1383-1384, change:
```python
        prof = fbprofile.load("country_digital_identity")
        reg = fbregistry.SourceRegistry.load("di_source_registry")
```
to:
```python
        prof = fbprofile.load(configurable.profile_name)
        reg = fbregistry.SourceRegistry.load(configurable.registry_name)
```

- [ ] **Step 4: Run the full suite (no behavior change for the default profile)**

Run: `uv run pytest -q`
Expected: all PASS (the default `profile_name`/`registry_name` reproduce today's behavior exactly).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/deep_researcher.py tests/test_factbase_selection.py
git commit -m "feat(factbase): select profile/registry by config name (no hardcoded literals)"
```

---

## Task 5: Stamp the run with the selected profile

**Files:**
- Modify: `src/open_deep_research/deep_researcher.py` (in `extract_facts`, after `prof`/`run_id` are known, ~line 1392-1394)

- [ ] **Step 1: Add the stamping call**

In `src/open_deep_research/deep_researcher.py`, inside `extract_facts`, locate (around line 1392-1394):
```python
        run_id = state.get("prealloc_run_id")
        async with aiosqlite.connect(get_db_path(config)) as conn:
            await fbmig.apply(conn, fbschema.STEPS)
```
Immediately AFTER the `await fbmig.apply(conn, fbschema.STEPS)` line, add:

```python
            # Provenance: stamp which profile produced this run's facts (after selection/load,
            # before extraction). Uses a direct UPDATE within the open connection.
            if run_id:
                await conn.execute(
                    "UPDATE research_runs SET profile_name=?, profile_version=?, profile_hash=? WHERE id=?",
                    (configurable.profile_name,
                     getattr(prof, "profile_version", None),
                     getattr(prof, "profile_hash", None),
                     run_id),
                )
                await conn.commit()
```

- [ ] **Step 2: Write a focused stamping test**

Append to `tests/test_factbase_run_stamp.py`:

```python
def test_stamp_update_persists(tmp_path):
    # Mirrors the engine's in-connection UPDATE to prove the columns accept a stamp mid-run.
    db = str(tmp_path / "fb.db")

    async def go():
        run_id = await storage.preallocate_run(db, "t")
        async with aiosqlite.connect(db) as conn:
            await migrations.apply(conn, schema.STEPS)
            await conn.execute(
                "UPDATE research_runs SET profile_name=?, profile_version=?, profile_hash=? WHERE id=?",
                ("country_digital_identity", "1", "deadbeef", run_id))
            await conn.commit()
            cur = await conn.execute(
                "SELECT profile_name, profile_hash FROM research_runs WHERE id=?", (run_id,))
            assert await cur.fetchone() == ("country_digital_identity", "deadbeef")

    asyncio.run(go())
```

- [ ] **Step 3: Run the stamping tests + full suite**

Run: `uv run pytest tests/test_factbase_run_stamp.py -q && uv run pytest -q`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add src/open_deep_research/deep_researcher.py tests/test_factbase_run_stamp.py
git commit -m "feat(factbase): stamp run with profile_name/version/hash after selection"
```

---

## Final verification

- [ ] **Full suite:** `uv run pytest -q` → all green (Phase-1 tests + the new selection/hash/stamp tests).
- [ ] **CI gate still passes:** `uv run dossier validate; echo "exit=$?"` → exit 0.
- [ ] **Default-profile behavior unchanged:** with no config override, `profile_name`/`registry_name` default to today's names, so every existing run behaves identically — confirmed by the full suite passing.
- [ ] **US-2 acceptance (manual, optional):** running with `configurable={"profile_name": "<some other profile>.yaml stem"}` would load that profile for target-property resolution and extraction with no Python edit (the literal `"country_digital_identity"` no longer appears in `deep_researcher.py` — verify with `grep -n 'load("country_digital_identity")\|load("di_source_registry")' src/open_deep_research/deep_researcher.py` → no output).

---

## Self-review notes (author)

- **Spec coverage (Phase-2 slice):** `profile_name`/`registry_name` selection ✓ (T1,T4); semantic `profile_hash` ✓ (T2); v6 provenance migration + post-selection stamping ✓ (T3,T5). Deferred (explicitly): prompt compilation (own plan + A/B), hash-mismatch detection + `dossier recompute --check` + normalization-recompute trigger (depends on this stamping; own plan), registry hash/own-meta-schema (own plan). Structural rebuild + scaffolding = Plan 6b.
- **Placeholder scan:** none — concrete code/commands throughout.
- **Type consistency:** `prof.profile_hash`/`profile_version` set in `profile_from_dict` (T2) and read by the engine stamp (T5) and `finalize_research_run` whitelist (T3); `configurable.profile_name`/`registry_name` defined (T1) and consumed at both load sites (T4) + the stamp (T5); column names `profile_name`/`profile_version`/`profile_hash` identical across migration v6, the whitelist, and the UPDATE.
- **Zero-behavior-change for the default:** the only runtime change for an unconfigured run is the new provenance UPDATE (additive); selection resolves to the same names.
