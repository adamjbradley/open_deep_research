# Profiles as Data — Plan 6a Phase 3 (Prompt Compilation from Schema) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compile the fact-extraction prompt **from the profile** — feeding the model each target property's kind, description, enum vocabulary, and qualifiers — instead of just a list of property names. Gated behind a config flag (default on) with the names-only path retained as the measured baseline.

**Architecture:** Add a `description` field to `PropertyDef` (the YAML already carries it; Phase 1 parsed-but-dropped it). Add a pure, testable `factbase/prompting.py` that renders a property catalog and the full extraction prompt. Wire it into `_make_fact_model_call` (`deep_researcher.py:1294-1329`) behind `Configuration.compile_extraction_prompt`. Also drop the hardcoded *"about a COUNTRY"* in favour of the profile's `entity_type`.

**Tech Stack:** Python 3.11, Pydantic v2, pytest. No new deps.

**Builds on:** Phases 1+2 (merged): YAML profiles, meta-schema, runtime selection, provenance.

**Scope:** Property-level descriptions + enum value lists + qualifiers in the prompt + the flag + a token-size warning. **Deferred:** per-enum-value descriptions in the prompt (kept simple here); the live A/B is a manual verification, not a unit test. Phase 4 (mismatch detection/recompute) and Plan 6b (scaffolding) unchanged.

---

## File structure

| File | Responsibility | Action |
|---|---|---|
| `src/open_deep_research/factbase/profile.py` | `PropertyDef.description` field | Modify (dataclass) |
| `src/open_deep_research/factbase/profile_schema.py` | populate `description` in `profile_from_dict` | Modify |
| `src/open_deep_research/factbase/prompting.py` | `compile_property_catalog` + `build_extraction_prompt` (pure) | Create |
| `src/open_deep_research/configuration.py` | `compile_extraction_prompt` flag | Modify |
| `src/open_deep_research/deep_researcher.py` | use `build_extraction_prompt` in `_make_fact_model_call` | Modify (1303-1312) |
| `tests/test_factbase_prompting.py` | catalog + prompt rendering, both modes | Create |
| `tests/test_factbase_description.py` | `description` populated on load | Create |

---

## Task 1: `PropertyDef.description`

**Files:**
- Modify: `src/open_deep_research/factbase/profile.py` (`PropertyDef` dataclass)
- Modify: `src/open_deep_research/factbase/profile_schema.py` (`profile_from_dict`)
- Test: `tests/test_factbase_description.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_factbase_description.py`:

```python
from open_deep_research.factbase import profile
from open_deep_research.factbase.profile_schema import profile_from_dict


def test_description_populated_from_dict():
    prof = profile_from_dict({
        "entity_type": "country", "version": "1",
        "properties": [{"name": "p", "kind": "name", "description": "hello world"}],
    })
    assert prof.property("p").description == "hello world"


def test_description_defaults_empty_and_real_profile_has_descriptions():
    prof = profile_from_dict({
        "entity_type": "country", "version": "1",
        "properties": [{"name": "p", "kind": "name"}],
    })
    assert prof.property("p").description == ""
    real = profile.load("country_digital_identity")
    assert real.property("scheme_status").description  # the YAML sets one
```

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_factbase_description.py -q`
Expected: FAIL (`AttributeError: 'PropertyDef' object has no attribute 'description'`).

- [ ] **Step 3: Add the field**

In `src/open_deep_research/factbase/profile.py`, in the `PropertyDef` dataclass, add a `description` field. Put it right after `value_kind`:

```python
@dataclass
class PropertyDef:
    name: str
    value_kind: str
    description: str = ""
    identity_qualifiers: list[str] = field(default_factory=list)
    required_qualifiers: list[str] = field(default_factory=list)
    qualifier_enums: dict[str, list[str]] = field(default_factory=dict)
    value_enum: list[str] | None = None
    trust_threshold: str = "reputable"
    value_aliases: dict[str, list[str]] = field(default_factory=dict)
```

(All other fields keep keyword-arg usage at the call site, so inserting a defaulted field is safe.)

- [ ] **Step 4: Populate it in `profile_from_dict`**

In `src/open_deep_research/factbase/profile_schema.py`, in `profile_from_dict`, add `description=p.description or ""` to the `PropertyDef(...)` construction (insert as the second argument, after `value_kind=p.kind,`):

```python
        PropertyDef(
            name=p.name,
            value_kind=p.kind,
            description=p.description or "",
            identity_qualifiers=list(p.identity_qualifiers),
            required_qualifiers=list(p.required_qualifiers),
            qualifier_enums={k: list(v) for k, v in p.qualifier_enums.items()},
            value_enum=p.enum_values(),
            trust_threshold=p.trust_threshold,
            value_aliases={k: list(v) for k, v in p.value_aliases.items()},
        )
```

- [ ] **Step 5: Run, verify pass + no regression**

Run: `uv run pytest tests/test_factbase_description.py tests/test_factbase_profile_schema.py tests/test_factbase_profile_roundtrip.py tests/test_factbase_profile_hash.py -q`
Expected: PASS. (The frozen-snapshot/round-trip tests don't compare `description`, and the hash excludes it, so both are unaffected.)

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/factbase/profile.py src/open_deep_research/factbase/profile_schema.py tests/test_factbase_description.py
git commit -m "feat(factbase): carry PropertyDef.description from the profile YAML"
```

---

## Task 2: `prompting.py` — pure catalog + prompt builder

**Files:**
- Create: `src/open_deep_research/factbase/prompting.py`
- Test: `tests/test_factbase_prompting.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_factbase_prompting.py`:

```python
from open_deep_research.factbase.profile_schema import profile_from_dict
from open_deep_research.factbase.prompting import build_extraction_prompt, compile_property_catalog

PROF = profile_from_dict({
    "entity_type": "country", "version": "1",
    "properties": [
        {"name": "scheme_status", "kind": "enum", "description": "maturity",
         "identity_qualifiers": ["basis"], "required_qualifiers": ["basis"],
         "qualifier_enums": {"basis": ["de_jure", "de_facto"]},
         "value_enum": ["operational", "mandatory"]},
        {"name": "scheme_name", "kind": "name", "description": "the scheme"},
    ],
})


def test_catalog_includes_kind_description_enums_qualifiers():
    cat = compile_property_catalog(PROF)
    assert "scheme_status" in cat and "(enum)" in cat
    assert "maturity" in cat                       # description
    assert "operational" in cat and "mandatory" in cat  # enum values
    assert "basis" in cat                          # qualifier


def test_catalog_respects_target_properties():
    cat = compile_property_catalog(PROF, target_properties=["scheme_name"])
    assert "scheme_name" in cat
    assert "scheme_status" not in cat


def test_compiled_prompt_uses_entity_type_and_catalog():
    p = build_extraction_prompt(PROF, None, "SRC TEXT", compiled=True)
    assert "COUNTRY" in p                           # entity_type, upper
    assert "scheme_status" in p and "operational" in p
    assert "SRC TEXT" in p
    assert "evidence_span" in p                      # guardrail preserved


def test_names_only_prompt_when_not_compiled():
    p = build_extraction_prompt(PROF, None, "SRC", compiled=False)
    assert "scheme_status" in p
    assert "operational" not in p                    # no enum vocab in baseline
    assert "Only use these property names" in p
```

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_factbase_prompting.py -q`
Expected: FAIL (`ModuleNotFoundError: ...prompting`).

- [ ] **Step 3: Implement**

Create `src/open_deep_research/factbase/prompting.py`:

```python
"""Render a domain profile into fact-extraction prompt text (pure, testable).

`compile_property_catalog` turns the selected properties into a human-readable
catalog (name, kind, description, enum vocabulary, qualifiers); `build_extraction_prompt`
wraps it (or, when compiled=False, the legacy names-only form) with the extraction
guardrails. Kept out of deep_researcher.py so it can be unit-tested without the graph.
"""
from __future__ import annotations

_SOURCE_CAP = 8000


def compile_property_catalog(prof, target_properties=None) -> str:
    names = target_properties or [pd.name for pd in prof.properties]
    lines = []
    for name in names:
        try:
            pd = prof.property(name)
        except KeyError:
            continue
        line = f"- {pd.name} ({pd.value_kind})"
        if getattr(pd, "description", ""):
            line += f": {pd.description}"
        if pd.value_enum:
            line += f" | allowed values: {pd.value_enum}"
        if pd.qualifier_enums:
            quals = "; ".join(f"{k}={v}" for k, v in pd.qualifier_enums.items())
            line += f" | qualifiers: {quals}"
        elif pd.identity_qualifiers:
            line += f" | qualifiers: {pd.identity_qualifiers}"
        lines.append(line)
    return "\n".join(lines)


def build_extraction_prompt(prof, target_properties, source_text, *, compiled: bool) -> str:
    src = (source_text or "")[:_SOURCE_CAP]
    entity = (prof.entity_type or "entity").upper()
    if compiled:
        catalog = compile_property_catalog(prof, target_properties)
        return (
            f"Extract facts about a {entity} from the source text below. "
            "Use ONLY these properties (name, kind, description, allowed values, qualifiers):\n"
            f"{catalog}\n\n"
            "Rules: emit a qualifier ONLY if the source explicitly states it (do not guess); "
            "for enum properties the value MUST be one of the listed allowed values; "
            "evidence_span MUST be a verbatim substring of the source text supporting the value; "
            "if nothing is stated, return an empty list.\n\nSOURCE:\n" + src
        )
    prop_names = target_properties or [pd.name for pd in prof.properties]
    return (
        f"Extract facts about a {entity} from the source text below. "
        f"Only use these property names: {prop_names}. "
        "Emit a qualifier ONLY if the source explicitly states it; otherwise omit it (do not guess). "
        "evidence_span MUST be a verbatim substring of the source text supporting the value. "
        "If nothing is stated, return an empty list.\n\nSOURCE:\n" + src
    )
```

- [ ] **Step 4: Run, verify pass**

Run: `uv run pytest tests/test_factbase_prompting.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/prompting.py tests/test_factbase_prompting.py
git commit -m "feat(factbase): pure prompt compiler (catalog + extraction prompt)"
```

---

## Task 3: `compile_extraction_prompt` config flag

**Files:**
- Modify: `src/open_deep_research/configuration.py`
- Test: `tests/test_factbase_prompting.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_factbase_prompting.py`:

```python
def test_compile_flag_default_on():
    from open_deep_research.configuration import Configuration
    assert Configuration().compile_extraction_prompt is True
```

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_factbase_prompting.py::test_compile_flag_default_on -q`
Expected: FAIL (no such field).

- [ ] **Step 3: Add the flag**

In `src/open_deep_research/configuration.py`, immediately after the `registry_name` Field (added in Phase 2), add:

```python
    compile_extraction_prompt: bool = Field(
        default=True,
        metadata={"x_oap_ui_config": {
            "type": "boolean",
            "default": True,
            "description": "Compile the fact-extraction prompt from the profile (property kinds, descriptions, enum vocabularies, qualifiers). When false, fall back to the names-only baseline.",
        }},
    )
```

- [ ] **Step 4: Run, verify pass**

Run: `uv run pytest tests/test_factbase_prompting.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/configuration.py tests/test_factbase_prompting.py
git commit -m "feat(config): compile_extraction_prompt flag (default on)"
```

---

## Task 4: Wire the compiler into `_make_fact_model_call`

**Files:**
- Modify: `src/open_deep_research/deep_researcher.py` (`_make_fact_model_call`, lines 1303-1312)

- [ ] **Step 1: Replace the inline prompt with the compiler**

In `src/open_deep_research/deep_researcher.py`, inside `_make_fact_model_call`'s `model_call`, replace the prompt-building block (currently lines ~1305-1312):

```python
            prop_names = target_properties or [pd.name for pd in prof.properties]
            prompt = (
                "Extract Digital-Identity facts about a COUNTRY from the source text below. "
                f"Only use these property names: {prop_names}. "
                "Emit a qualifier ONLY if the source explicitly states it; otherwise omit it (do not guess). "
                "evidence_span MUST be a verbatim substring of the source text supporting the value. "
                "If nothing is stated, return an empty list.\n\nSOURCE:\n" + (source_text or "")[:8000]
            )
```

with:

```python
            from open_deep_research.factbase.prompting import build_extraction_prompt
            prompt = build_extraction_prompt(
                prof, target_properties, source_text,
                compiled=configurable.compile_extraction_prompt,
            )
            if configurable.compile_extraction_prompt and len(prompt) > 12000:
                logger.warning(
                    "Compiled extraction prompt is large (%d chars) for entity_type=%s; "
                    "consider trimming the profile.", len(prompt), prof.entity_type)
```

- [ ] **Step 2: Import check**

Run: `uv run python -c "import open_deep_research.deep_researcher"`
Expected: no error.

- [ ] **Step 3: Full suite (no regression; default flag on)**

Run: `uv run pytest -q`
Expected: all PASS.

- [ ] **Step 4: Confirm the hardcoded domain string is gone**

Run: `grep -n "Digital-Identity facts about a COUNTRY" src/open_deep_research/deep_researcher.py`
Expected: NO output (the compiler now derives the entity from the profile).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/deep_researcher.py
git commit -m "feat(factbase): extract using a profile-compiled prompt (flagged, default on)"
```

---

## Final verification

- [ ] **Full suite:** `uv run pytest -q` → all green.
- [ ] **CI gate:** `uv run dossier validate; echo "exit=$?"` → exit 0.
- [ ] **No hardcoded domain in the prompt:** `grep -rn "about a COUNTRY" src/open_deep_research/deep_researcher.py` → empty.
- [ ] **A/B (manual, optional — needs a real run, Tavily wired):** run the standing India brief twice — once with `compile_extraction_prompt=true` (default), once `false` — via the `run-research-query` skill. Compare facts captured **and** false/unsupported facts; the compiled path should not increase the latter. Record the numbers in the PR description. (Not automated — it needs the LLM + network.)

---

## Self-review notes (author)

- **Spec coverage (Phase-3 slice):** enum vocabularies + descriptions compiled into the prompt ✓ (T1,T2,T4); config flag with names-only baseline retained ✓ (T3,T2's `compiled=False`); token-size warning ✓ (T4); entity_type derived from the profile (drops hardcoded "COUNTRY") ✓ (T2,T4). Deferred: per-enum-value descriptions in the prompt; the live A/B (manual verification).
- **Placeholder scan:** none.
- **Type consistency:** `PropertyDef.description` set in `profile_from_dict` (T1) and read by `compile_property_catalog` (T2); `build_extraction_prompt(prof, target_properties, source_text, *, compiled)` defined in T2 and called in T4 with `configurable.compile_extraction_prompt`; the names-only branch reproduces today's prompt text (minus the now-dynamic entity name), so `compiled=False` is a faithful baseline.
