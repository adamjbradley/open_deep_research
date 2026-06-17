# Enum Fidelity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `kind: enum` properties represent set-valued facts (a subset of the allowed values) and open-world facts (values outside the known list kept verbatim), so `biometric_capture` stops collapsing fingerprint+iris+photo into a lossy `multi`.

**Architecture:** Two orthogonal boolean modifiers — `multi` and `open` — are added to enum properties (not new kinds). `multi` makes the value a sorted, comma-joined set; `open` allows literals outside `value_enum`. The flags thread through schema validation, value validation, canonicalization (dedup key), and the extraction prompt. A validation sweep is added to the structural rebuild so existing values that no longer validate (e.g. the retired `multi` member) get soft-deleted and re-researched.

**Tech Stack:** Python 3, Pydantic v2 (`profile_schema.py`), dataclasses (`profile.py`), `aiosqlite` (rebuild/tests), `pytest` + `asyncio` for tests, YAML profiles.

Spec: `docs/superpowers/specs/2026-06-17-enum-fidelity-design.md`

## Global Constraints

- Flags `multi` / `open` are valid **only** when `kind: enum`; both require `value_enum` present. Using either on a non-enum kind, or without `value_enum`, must raise at schema-validation time.
- No DB schema/column migration: the fact `value` stays a single `TEXT` column holding the model's verbatim string. The **canonical** key for dedup/grouping is the **sorted, deduplicated, lowercased members joined by `", "`**, computed by `canonical_value` (not written back to the raw `value` column).
- Attribute naming: the YAML keys are `multi` and `open`. On `PropertyModel` (pydantic) the fields are `multi` and `open`. On `PropertyDef` (dataclass) they are `multi` and `open_world` (avoid shadowing the `open` builtin). `profile_from_dict` maps `open` → `open_world`.
- `canonical_value` must remain deterministic and never raise (existing contract).
- TDD: write the failing test first, watch it fail, implement minimally, watch it pass, commit. Run tests with `uv run pytest`.
- `none`/`multi` are NOT members of a `multi` enum; an empty/absent value represents "none captured" and validates as true.

---

### Task 1: Schema flags (`multi` / `open`) on enum properties

**Files:**
- Modify: `src/open_deep_research/factbase/profile_schema.py` (add fields to `PropertyModel`, extend `_check`, map in `profile_from_dict`, add to semantic hash)
- Modify: `src/open_deep_research/factbase/profile.py:8-23` (add `multi` / `open_world` fields to `PropertyDef`)
- Test: `tests/test_factbase_profile_schema.py`

**Interfaces:**
- Produces: `PropertyDef.multi: bool = False`, `PropertyDef.open_world: bool = False`. `PropertyModel` accepts YAML keys `multi: bool` and `open: bool`. The per-property semantic-hash dict gains keys `"multi"` and `"open"`.
- Consumes: nothing from other tasks.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_factbase_profile_schema.py`:

```python
def test_multi_and_open_flags_build_and_surface():
    prof = profile_from_dict({
        "entity_type": "country", "version": "1",
        "properties": [
            {"name": "biometric_capture", "kind": "enum", "multi": True,
             "value_enum": ["photo", "fingerprint", "iris", "face"]},
            {"name": "role", "kind": "enum", "open": True,
             "value_enum": ["sender", "receiver"]},
        ],
    })
    bio = prof.property("biometric_capture")
    assert bio.multi is True and bio.open_world is False
    role = prof.property("role")
    assert role.open_world is True and role.multi is False


def test_multi_on_non_enum_rejected():
    bad = {"entity_type": "country", "properties": [
        {"name": "x", "kind": "name", "multi": True}]}
    with pytest.raises(ValueError, match="multi.*only allowed for kind 'enum'"):
        profile_from_dict(bad)


def test_open_without_value_enum_rejected():
    bad = {"entity_type": "country", "properties": [
        {"name": "x", "kind": "enum", "open": True}]}
    with pytest.raises(ValueError, match="open.*requires value_enum"):
        profile_from_dict(bad)


def test_toggling_multi_changes_semantic_hash():
    base = {"entity_type": "country", "properties": [
        {"name": "b", "kind": "enum", "value_enum": ["photo", "iris"]}]}
    multi = {"entity_type": "country", "properties": [
        {"name": "b", "kind": "enum", "multi": True, "value_enum": ["photo", "iris"]}]}
    assert profile_from_dict(base).profile_hash != profile_from_dict(multi).profile_hash
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_factbase_profile_schema.py -k "multi or open or semantic_hash" -v`
Expected: FAIL — `PropertyModel` has no `multi`/`open` fields (extra fields ignored → `bio.multi` AttributeError) and no validation messages.

- [ ] **Step 3: Add fields + validation to `PropertyModel`**

In `src/open_deep_research/factbase/profile_schema.py`, add two fields to `PropertyModel` (after `value_enum`, near line 32):

```python
    multi: bool = False
    open: bool = False
```

Extend the `_check` validator (after the existing `value_enum` guard at line 41):

```python
        if self.multi and self.kind != "enum":
            raise ValueError(f"property {self.name!r}: multi only allowed for kind 'enum'")
        if self.open and self.kind != "enum":
            raise ValueError(f"property {self.name!r}: open only allowed for kind 'enum'")
        if (self.multi or self.open) and self.value_enum is None:
            raise ValueError(f"property {self.name!r}: multi/open requires value_enum")
```

- [ ] **Step 4: Map fields into `PropertyDef` and the semantic hash**

In `src/open_deep_research/factbase/profile.py`, add to the `PropertyDef` dataclass (after `value_aliases`, line 23):

```python
    multi: bool = False
    open_world: bool = False
```

In `profile_schema.py::profile_from_dict`, add to the `PropertyDef(...)` constructor (after `value_aliases=...`, line 108):

```python
            multi=p.multi,
            open_world=p.open,
```

And in the `semantic["properties"]` dict comprehension (after the `value_enum` key, line 125):

```python
                "multi": pd.multi,
                "open": pd.open_world,
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_factbase_profile_schema.py -v`
Expected: PASS (new tests + all existing schema tests).

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/factbase/profile_schema.py src/open_deep_research/factbase/profile.py tests/test_factbase_profile_schema.py
git commit -m "feat(factbase): multi/open enum flags in profile schema + hash"
```

---

### Task 2: Value validation for set/open enums (`PropertyDef.validate`)

**Files:**
- Modify: `src/open_deep_research/factbase/profile.py:39-54` (`PropertyDef.validate`)
- Test: `tests/test_factbase_profile.py`

**Interfaces:**
- Consumes: `PropertyDef.multi`, `PropertyDef.open_world` (Task 1).
- Produces: `validate(value: str) -> bool` honoring set/open semantics. Member split rule (reused conceptually by Task 3): split on `,`, strip, drop empties, lowercase.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_factbase_profile.py`:

```python
from open_deep_research.factbase.profile_schema import profile_from_dict

def _prop(**kw):
    base = {"name": "p", "kind": "enum", "value_enum": ["photo", "fingerprint", "iris"]}
    base.update(kw)
    return profile_from_dict({"entity_type": "c", "properties": [base]}).property("p")


def test_multi_closed_accepts_valid_subset_rejects_junk():
    p = _prop(multi=True)
    assert p.validate("fingerprint, iris") is True
    assert p.validate("iris,photo") is True
    assert p.validate("fingerprint, asdf") is False     # one bad member rejects whole fact
    assert p.validate("") is True                        # empty set == none captured


def test_multi_open_keeps_unknown_member():
    p = _prop(multi=True, open=True)
    assert p.validate("fingerprint, palmprint") is True  # palmprint not in enum, allowed


def test_single_open_accepts_literal_outside_enum():
    p = _prop(open=True)
    assert p.validate("hub") is True                     # not in enum, allowed by open
    assert p.validate("photo") is True


def test_single_closed_unchanged():
    p = _prop()
    assert p.validate("photo") is True
    assert p.validate("hub") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_factbase_profile.py -k "multi or open or single_closed" -v`
Expected: FAIL — current `validate` only does single-closed membership; `"fingerprint, iris"` is not a member so returns False.

- [ ] **Step 3: Rewrite the enum branch of `validate`**

In `src/open_deep_research/factbase/profile.py`, replace the enum branch (lines 52-53) with:

```python
        if self.value_kind == "enum" and self.value_enum is not None:
            if self.multi:
                members = [m.strip().lower() for m in v.split(",") if m.strip()]
                if not members:
                    return True  # empty set == none captured
                if self.open_world:
                    return True  # any non-empty member set ok; unknowns kept verbatim
                allowed = {e.lower() for e in self.value_enum}
                return all(m in allowed for m in members)
            if self.open_world:
                return bool(v)  # single, open: any non-empty literal
            return v.lower() in {e.lower() for e in self.value_enum}
```

(`v` is the stripped value from line 40.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_factbase_profile.py -v`
Expected: PASS (new tests + existing profile tests).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/profile.py tests/test_factbase_profile.py
git commit -m "feat(factbase): validate set-valued and open-world enum values"
```

---

### Task 3: Canonicalization set branch (`identity.canonical_value`)

**Files:**
- Modify: `src/open_deep_research/factbase/identity.py:59-64` (enum branch of `canonical_value`)
- Test: `tests/test_factbase_identity.py`

**Interfaces:**
- Consumes: `property_def.multi`, `property_def.value_enum` (Task 1) via `getattr` (the function already uses `getattr` so it tolerates plain objects).
- Produces: a `multi` enum value canonicalizes to `(", ".join(sorted_lowercased_unique_members), None)` — order-independent so `{iris, photo}` and `{photo, iris}` dedupe.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_factbase_identity.py`:

```python
from open_deep_research.factbase import identity
from open_deep_research.factbase.profile_schema import profile_from_dict

def _multi_prop():
    return profile_from_dict({"entity_type": "c", "properties": [
        {"name": "b", "kind": "enum", "multi": True,
         "value_enum": ["photo", "fingerprint", "iris"]}]}).property("b")


def test_multi_enum_canonical_is_order_independent():
    p = _multi_prop()
    a = identity.canonical_value(p, "iris, photo", None)
    b = identity.canonical_value(p, "photo,  IRIS", None)
    assert a == b == ("iris, photo", None)


def test_multi_enum_dedupes_and_sorts_members():
    p = _multi_prop()
    assert identity.canonical_value(p, "photo, photo, fingerprint", None) == (
        "fingerprint, photo", None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_factbase_identity.py -k "multi_enum" -v`
Expected: FAIL — current enum branch returns the whole lowercased string `"iris, photo"` unsorted, so the two orderings differ.

- [ ] **Step 3: Add the set branch**

In `src/open_deep_research/factbase/identity.py`, at the start of the `if kind == "enum":` block (line 59), insert before the existing single-value logic:

```python
    if kind == "enum" and getattr(property_def, "multi", False):
        members = sorted({m.strip().lower() for m in raw.split(",") if m.strip()})
        return (", ".join(members), None)
```

The existing `if kind == "enum":` block (single-value) stays as the fallthrough for non-multi enums.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_factbase_identity.py -v`
Expected: PASS (new tests + existing identity/value-normalization tests).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/identity.py tests/test_factbase_identity.py
git commit -m "feat(factbase): order-independent canonical key for multi enums"
```

---

### Task 4: Extraction-prompt hints for set/open enums (`prompting.py`)

**Files:**
- Modify: `src/open_deep_research/factbase/prompting.py:13-37` (`compile_property_catalog`) and `:40-53` (`build_extraction_prompt` global rule)
- Test: `tests/test_factbase_prompting.py`

**Interfaces:**
- Consumes: `pd.multi`, `pd.open_world` (Task 1).
- Produces: catalog lines whose kind annotation reflects the flags, and "known values" wording when open.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_factbase_prompting.py`:

```python
def _cat(**flags):
    base = {"name": "b", "kind": "enum", "description": "modality",
            "value_enum": ["photo", "iris"]}
    base.update(flags)
    prof = profile_from_dict({"entity_type": "c", "properties": [base]})
    return compile_property_catalog(prof)


def test_multi_closed_line_says_select_all():
    cat = _cat(multi=True)
    assert "select all that apply" in cat
    assert "allowed values" in cat


def test_multi_open_line_says_others_verbatim_and_known_values():
    cat = _cat(multi=True, open=True)
    assert "select all that apply" in cat
    assert "list others verbatim" in cat
    assert "known values" in cat and "allowed values" not in cat


def test_single_open_line_says_literal_and_known_values():
    cat = _cat(open=True)
    assert "give the literal" in cat
    assert "known values" in cat


def test_single_closed_line_unchanged():
    cat = _cat()
    assert "(enum)" in cat and "allowed values" in cat
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_factbase_prompting.py -k "multi or open or single_closed" -v`
Expected: FAIL — catalog currently emits `(enum)` and `allowed values` unconditionally.

- [ ] **Step 3: Add flag-aware rendering to `compile_property_catalog`**

In `src/open_deep_research/factbase/prompting.py`, replace the line-building block (lines 21-30) with:

```python
        multi = getattr(pd, "multi", False)
        open_world = getattr(pd, "open_world", False)
        kind_label = pd.value_kind
        if pd.value_kind == "enum" and (multi or open_world):
            hints = []
            if multi:
                hints.append("select all that apply")
            if open_world:
                hints.append(
                    "list others verbatim if outside this set" if multi
                    else "use a listed value or give the literal if none fit"
                )
            kind_label = "enum, " + "; ".join(hints)
        line = f"- {pd.name} ({kind_label})"
        if getattr(pd, "description", ""):
            line += f": {pd.description}"
        if pd.value_enum:
            label = "known values" if open_world else "allowed values"
            descs = getattr(pd, "value_enum_descriptions", None) or {}
            if descs:
                vals = ", ".join(f"{v} ({descs[v]})" if v in descs else v for v in pd.value_enum)
                line += f" | {label}: [{vals}]"
            else:
                line += f" | {label}: {pd.value_enum}"
```

- [ ] **Step 4: Reword the global enum rule**

In `build_extraction_prompt`, replace the enum clause in the compiled-prompt rules (line 50-51, the `"for enum properties the value MUST be one of the listed allowed values; "` fragment) with:

```python
            "for enum properties use the listed values; when a property says 'select all "
            "that apply', return every applicable value separated by commas; when it allows "
            "literals, you may give a value outside the list; "
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_factbase_prompting.py -v`
Expected: PASS (new tests + existing prompting tests, which assert `(enum)` and `allowed values` for plain enums — still satisfied by the single-closed path).

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/factbase/prompting.py tests/test_factbase_prompting.py
git commit -m "feat(factbase): extraction-prompt hints for set/open enums"
```

---

### Task 5: Validation sweep in structural rebuild (`rebuild.py`)

**Files:**
- Modify: `src/open_deep_research/factbase/rebuild.py:25-26` (stats init) and `:41-48` (per-row loop)
- Test: `tests/test_factbase_rebuild.py`

**Interfaces:**
- Consumes: `PropertyDef.validate` with set/open semantics (Task 2).
- Produces: `rebuild_structural` soft-deletes rows whose stored `value` no longer validates and reports them as `stats["invalidated"]`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_factbase_rebuild.py` (reuses the file's `_seed_source`, `_seed_fact`, `REG` helpers):

```python
def test_rebuild_soft_deletes_values_that_no_longer_validate(tmp_path):
    db = str(tmp_path / "fb.db")

    async def go():
        async with aiosqlite.connect(db) as conn:
            await storage._ensure_schema(conn)
            await migrations.apply(conn, schema.STEPS)
            sa = await _seed_source(conn, "https://a.example/x")
            # Old profile allowed the 'multi' member; seed a now-stale value + a valid sibling.
            await _seed_fact(conn, property_name="biometric_capture", quals={},
                             value="multi", source_id=sa, tuple_key="TK_STALE")
            await _seed_fact(conn, property_name="biometric_capture", quals={},
                             value="fingerprint, iris", source_id=sa, tuple_key="TK_OK")
            await conn.commit()

            new_prof = profile_from_dict({"entity_type": "country", "version": "2", "properties": [
                {"name": "biometric_capture", "kind": "enum", "multi": True,
                 "value_enum": ["photo", "fingerprint", "iris", "face"]}]})

            stats = await rebuild_structural(conn, new_prof, REG)
            assert stats["invalidated"] == 1

            stale = await (await conn.execute(
                "SELECT soft_deleted_at FROM fact WHERE value='multi'")).fetchone()
            assert stale[0] is not None  # soft-deleted
            ok = await (await conn.execute(
                "SELECT soft_deleted_at FROM fact WHERE value='fingerprint, iris'")).fetchone()
            assert ok[0] is None         # valid sibling survives

    asyncio.run(go())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_factbase_rebuild.py -k "no_longer_validate" -v`
Expected: FAIL — `KeyError: 'invalidated'` (stat absent) and the `multi` row is retained, not soft-deleted.

- [ ] **Step 3: Add the stat and the sweep**

In `src/open_deep_research/factbase/rebuild.py`, add `"invalidated": 0` to the `stats` dict (line 25-26):

```python
    stats = {"tuple_keys_changed": 0, "conflicts_opened": 0,
             "promoted": 0, "demoted": 0, "orphaned": 0, "invalidated": 0}
```

In the per-row loop, immediately after the orphan-handling block (after line 48, before `quals = json.loads(...)`), insert:

```python
        if not pd.validate(r["value"]):
            stats["invalidated"] += 1
            await conn.execute(
                "UPDATE fact SET soft_deleted_at=? WHERE id=?", (now, r["id"]))
            continue
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_factbase_rebuild.py -v`
Expected: PASS (new test + existing rebuild tests, whose seeded values all still validate under their profiles).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/rebuild.py tests/test_factbase_rebuild.py
git commit -m "feat(factbase): rebuild soft-deletes values that no longer validate"
```

---

### Task 6: Reference migration of `biometric_capture`

**Files:**
- Modify: `src/open_deep_research/factbase/profiles/country_digital_identity.yaml:32-35`
- Test: `tests/test_factbase_profile_roundtrip.py`

**Interfaces:**
- Consumes: all prior tasks (the migrated profile exercises `multi`, validation, canonicalization, prompting).
- Produces: a `biometric_capture` property that is `multi`, closed, with members `[photo, fingerprint, iris, face]` and no `multi`/`none` members.

- [ ] **Step 1: Write the failing regression test**

Add to `tests/test_factbase_profile_roundtrip.py`:

```python
from open_deep_research.factbase import profile as _profile

def test_biometric_capture_is_multi_enum_without_catchall_members():
    prof = _profile.load("country_digital_identity")
    bio = prof.property("biometric_capture")
    assert bio.multi is True
    assert bio.open_world is False
    assert bio.value_enum == ["photo", "fingerprint", "iris", "face"]
    assert "multi" not in bio.value_enum and "none" not in bio.value_enum
    # set value validates; retired catch-all does not
    assert bio.validate("fingerprint, iris") is True
    assert bio.validate("multi") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_factbase_profile_roundtrip.py -k "biometric_capture_is_multi" -v`
Expected: FAIL — current YAML has `value_enum: [none, photo, fingerprint, iris, multi]` and no `multi` flag.

- [ ] **Step 3: Edit the profile YAML**

In `src/open_deep_research/factbase/profiles/country_digital_identity.yaml`, replace the `biometric_capture` property (lines 32-35):

```yaml
  - name: biometric_capture
    kind: enum
    multi: true
    description: "Biometric modalities captured at enrolment (one or more)."
    value_enum: [photo, fingerprint, iris, face]
```

- [ ] **Step 4: Run test + full factbase suite to verify it passes**

Run: `uv run pytest tests/test_factbase_profile_roundtrip.py -v && uv run pytest tests/ -k factbase -q`
Expected: PASS — new regression test passes and no existing factbase test regresses.

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/profiles/country_digital_identity.yaml tests/test_factbase_profile_roundtrip.py
git commit -m "feat(factbase): migrate biometric_capture to multi enum"
```

---

## Self-Review

**Spec coverage:**
- Section 1 (data model: flags, validation, hash, none-as-empty-set) → Task 1 + Task 6.
- Section 2 (storage form, canonical_value set branch, validate set/open) → Task 2 (validate) + Task 3 (canonical).
- Section 3 (prompting hints + global rule) → Task 4.
- Section 4 (reference migration + verified rebuild gap → validation sweep) → Task 5 (sweep) + Task 6 (migration).
- Section 5 (testing) → tests embedded in every task; the rebuild DB-level test and reference-migration regression are Tasks 5 & 6.
- Out-of-scope items (ordinal collapse, CBDC `hybrid` conversion, DB column migration) → correctly absent.

**Placeholder scan:** No TBD/TODO; every code step shows the actual code; commands have expected output. Clear.

**Type consistency:** `multi` / `open` (pydantic `PropertyModel`) → `multi` / `open_world` (`PropertyDef`) mapping is stated in Global Constraints and applied identically in Tasks 1-6. `stats["invalidated"]` defined in Task 5 init and asserted in its test. `canonical_value` set branch returns `(str, None)` matching the existing `tuple[str, str|None]` signature. Consistent.
