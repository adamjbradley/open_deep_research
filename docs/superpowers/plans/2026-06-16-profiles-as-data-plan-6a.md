# Profiles as Data — Plan 6a (Phase 1: Externalize to Validated YAML) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the factbase domain profile and source registry from Python modules to validated, runtime-loaded YAML data files — with the *default* profile still selected, so there is **zero graph-behavior change**.

**Architecture:** Keep the `Profile`/`PropertyDef` dataclasses and the `load(name) -> Profile` / `SourceRegistry.load(name)` call signatures unchanged. The loaders read a `.yaml` file (package-data-safe via `importlib.resources`), validate it through a Pydantic meta-schema, and build the existing dataclasses. A golden round-trip test proves the `.py → .yaml` migration is lossless (including the Aadhaar `value_aliases`). A new `dossier validate` CLI lints every profile/registry file and gates CI.

**Tech Stack:** Python 3.11, PyYAML (`yaml.safe_load`), Pydantic v2 (already a dependency via `configuration.py`), `importlib.resources`, pytest, argparse/asyncio (existing `dossier` CLI).

**Scope note:** This is Plan 6a **Phase 1** from the converged spec (`docs/superpowers/specs/2026-06-16-profiles-as-data-design.md`). It deliberately excludes runtime `profile_name` selection, prompt compilation, provenance stamping, and recompute (Plan 6a Phase 2) and the structural rebuild + scaffolding (Plan 6b). `profile_hash` is therefore **not** computed here — it arrives with provenance stamping in Phase 2.

---

## File structure

| File | Responsibility | Action |
|---|---|---|
| `src/open_deep_research/factbase/profile_schema.py` | Pydantic meta-schema (`ProfileModel`/`PropertyModel`) + `profile_from_dict()` builder | Create |
| `src/open_deep_research/factbase/profile.py` | `load(name)` reads+validates YAML, builds `Profile` | Modify (`load` only) |
| `src/open_deep_research/factbase/registry_schema.py` | Pydantic meta-schema for the registry + `registry_from_dict()` | Create |
| `src/open_deep_research/factbase/registry.py` | `SourceRegistry.load(name)` reads+validates YAML | Modify (`load` only) |
| `src/open_deep_research/factbase/profiles/country_digital_identity.yaml` | The DI profile as data | Create |
| `src/open_deep_research/factbase/profiles/di_source_registry.yaml` | The source registry as data | Create |
| `src/open_deep_research/factbase/dossier.py` | add `validate` subcommand (lints all profile/registry YAML) | Modify |
| `pyproject.toml` | declare `pyyaml`; ship `*.yaml` as package-data | Modify |
| `tests/test_factbase_profile_schema.py` | meta-schema accept/reject | Create |
| `tests/test_factbase_profile_roundtrip.py` | golden `.py == .yaml` parity | Create |
| `tests/test_factbase_registry_yaml.py` | registry YAML parity + validation | Create |
| `tests/test_dossier_validate.py` | `dossier validate` exit behavior | Create |

The two `.py` profile modules (`profiles/country_digital_identity.py`, `profiles/di_source_registry.py`) are **kept until Task 7**, where they are deleted after parity is proven.

---

## Task 1: Declare PyYAML and ship YAML as package-data

**Files:**
- Modify: `pyproject.toml:11-49` (dependencies), `pyproject.toml:69-70` (package-data)

- [ ] **Step 1: Add `pyyaml` to dependencies**

In `pyproject.toml`, inside the `[project] dependencies` list (after `"pandas>=2.3.1",` on line 48), add:

```toml
    "pyyaml>=6.0",
```

- [ ] **Step 2: Ship YAML files inside the package**

Replace the `[tool.setuptools.package-data]` block (lines 69-70):

```toml
[tool.setuptools.package-data]
"*" = ["py.typed"]
"open_deep_research.factbase.profiles" = ["*.yaml"]
```

- [ ] **Step 3: Sync the environment**

Run: `uv sync`
Expected: resolves and installs with `pyyaml` present (no error).

- [ ] **Step 4: Verify yaml imports**

Run: `uv run python -c "import yaml; print(yaml.__version__)"`
Expected: prints a version (e.g. `6.0.2`).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: declare pyyaml and ship factbase profile YAML as package-data"
```

---

## Task 2: Pydantic meta-schema for profiles

**Files:**
- Create: `src/open_deep_research/factbase/profile_schema.py`
- Test: `tests/test_factbase_profile_schema.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_factbase_profile_schema.py`:

```python
import pytest

from open_deep_research.factbase.profile_schema import profile_from_dict

VALID = {
    "entity_type": "country",
    "version": "1",
    "properties": [
        {
            "name": "scheme_status",
            "kind": "enum",
            "identity_qualifiers": ["basis"],
            "required_qualifiers": ["basis"],
            "qualifier_enums": {"basis": ["de_jure", "de_facto"]},
            "value_enum": [
                {"value": "operational", "description": "issuing at scale"},
                "mandatory",
            ],
        },
        {"name": "scheme_name", "kind": "name", "value_aliases": {"aadhaar": ["uidai"]}},
    ],
}


def test_valid_profile_builds_dataclass_with_enum_values_flattened():
    prof = profile_from_dict(VALID)
    assert prof.entity_type == "country"
    status = prof.property("scheme_status")
    assert status.value_enum == ["operational", "mandatory"]  # {value,...} flattened
    assert status.validate("mandatory") is True
    assert prof.property("scheme_name").aliases_for("uidai") == "aadhaar"


def test_unknown_kind_rejected():
    bad = {"entity_type": "country", "properties": [{"name": "x", "kind": "wat"}]}
    with pytest.raises(ValueError, match="unknown kind"):
        profile_from_dict(bad)


def test_value_enum_on_non_enum_rejected():
    bad = {"entity_type": "country",
           "properties": [{"name": "x", "kind": "name", "value_enum": ["a"]}]}
    with pytest.raises(ValueError, match="value_enum only allowed"):
        profile_from_dict(bad)


def test_required_qualifier_not_in_identity_rejected():
    bad = {"entity_type": "country",
           "properties": [{"name": "x", "kind": "name", "required_qualifiers": ["basis"]}]}
    with pytest.raises(ValueError, match="required_qualifiers"):
        profile_from_dict(bad)


def test_qualifier_enums_key_not_declared_rejected():
    bad = {"entity_type": "country",
           "properties": [{"name": "x", "kind": "name", "qualifier_enums": {"basis": ["a"]}}]}
    with pytest.raises(ValueError, match="qualifier_enums"):
        profile_from_dict(bad)


def test_duplicate_property_names_rejected():
    bad = {"entity_type": "country",
           "properties": [{"name": "x", "kind": "name"}, {"name": "x", "kind": "name"}]}
    with pytest.raises(ValueError, match="duplicate property"):
        profile_from_dict(bad)


def test_empty_entity_type_rejected():
    bad = {"entity_type": "  ", "properties": [{"name": "x", "kind": "name"}]}
    with pytest.raises(ValueError, match="entity_type"):
        profile_from_dict(bad)


def test_overlapping_value_aliases_rejected():
    bad = {"entity_type": "country", "properties": [
        {"name": "x", "kind": "name", "value_aliases": {"a": ["dup"], "b": ["dup"]}}]}
    with pytest.raises(ValueError, match="multiple canonicals"):
        profile_from_dict(bad)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_factbase_profile_schema.py -q`
Expected: FAIL with `ModuleNotFoundError: open_deep_research.factbase.profile_schema`.

- [ ] **Step 3: Implement the meta-schema**

Create `src/open_deep_research/factbase/profile_schema.py`:

```python
"""Pydantic meta-schema for domain profiles, plus a builder to the runtime dataclasses.

Validates a parsed YAML profile (structure, enums, qualifier coherence, alias
integrity) and constructs the existing ``Profile``/``PropertyDef`` dataclasses.
Kept separate from ``profile.py`` to avoid an import cycle (loaded lazily there).
"""
from __future__ import annotations

from typing import Optional, Union

from pydantic import BaseModel, model_validator

from .profile import Profile, PropertyDef

_VALID_KINDS = {"name", "enum", "percentage", "boolean", "name_year"}


class _EnumValue(BaseModel):
    value: str
    description: Optional[str] = None


class PropertyModel(BaseModel):
    name: str
    kind: str
    description: Optional[str] = None
    identity_qualifiers: list[str] = []
    required_qualifiers: list[str] = []
    qualifier_enums: dict[str, list[str]] = {}
    value_enum: Optional[list[Union[str, _EnumValue]]] = None
    trust_threshold: str = "reputable"
    value_aliases: dict[str, list[str]] = {}

    @model_validator(mode="after")
    def _check(self) -> "PropertyModel":
        if self.kind not in _VALID_KINDS:
            raise ValueError(f"property {self.name!r}: unknown kind {self.kind!r}")
        if self.value_enum is not None and self.kind != "enum":
            raise ValueError(f"property {self.name!r}: value_enum only allowed for kind 'enum'")
        missing = set(self.required_qualifiers) - set(self.identity_qualifiers)
        if missing:
            raise ValueError(
                f"property {self.name!r}: required_qualifiers {sorted(missing)} not in identity_qualifiers"
            )
        known = set(self.identity_qualifiers) | set(self.required_qualifiers)
        undeclared = set(self.qualifier_enums) - known
        if undeclared:
            raise ValueError(
                f"property {self.name!r}: qualifier_enums keys {sorted(undeclared)} are not declared qualifiers"
            )
        seen: dict[str, str] = {}
        for canonical, variants in self.value_aliases.items():
            for surface in [canonical, *variants]:
                key = surface.strip().lower()
                if key in seen and seen[key] != canonical:
                    raise ValueError(
                        f"property {self.name!r}: alias {surface!r} maps to multiple canonicals"
                    )
                seen[key] = canonical
        return self

    def enum_values(self) -> Optional[list[str]]:
        if self.value_enum is None:
            return None
        return [e.value if isinstance(e, _EnumValue) else e for e in self.value_enum]


class ProfileModel(BaseModel):
    entity_type: str
    version: str = "1"
    notes: Optional[str] = None
    properties: list[PropertyModel]

    @model_validator(mode="after")
    def _check(self) -> "ProfileModel":
        if not self.entity_type.strip():
            raise ValueError("entity_type must be non-empty")
        names = [p.name for p in self.properties]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate property names: {dupes}")
        return self


def profile_from_dict(data: dict) -> Profile:
    """Validate a parsed profile dict and build the runtime ``Profile`` dataclass."""
    model = ProfileModel.model_validate(data)
    props = [
        PropertyDef(
            name=p.name,
            value_kind=p.kind,
            identity_qualifiers=list(p.identity_qualifiers),
            required_qualifiers=list(p.required_qualifiers),
            qualifier_enums={k: list(v) for k, v in p.qualifier_enums.items()},
            value_enum=p.enum_values(),
            trust_threshold=p.trust_threshold,
            value_aliases={k: list(v) for k, v in p.value_aliases.items()},
        )
        for p in model.properties
    ]
    prof = Profile(entity_type=model.entity_type, properties=props)
    prof.profile_version = model.version  # carried as an attribute; hash arrives in Phase 2
    return prof
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_factbase_profile_schema.py -q`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/profile_schema.py tests/test_factbase_profile_schema.py
git commit -m "feat(factbase): Pydantic meta-schema + profile_from_dict builder"
```

---

## Task 3: YAML-reading `profile.load()`

**Files:**
- Modify: `src/open_deep_research/factbase/profile.py:59-61` (`load` only)
- Create: `src/open_deep_research/factbase/profiles/country_digital_identity.yaml`
- Test: `tests/test_factbase_profile_roundtrip.py`

- [ ] **Step 1: Author the YAML profile (lossless translation of the `.py`)**

Create `src/open_deep_research/factbase/profiles/country_digital_identity.yaml`:

```yaml
entity_type: country
version: "1"
notes: "Digital-Identity pillar for the country entity type (Feature Spec 2.1)."
properties:
  - name: foundational_id_scheme
    kind: name
    description: "The country's primary national/foundational ID scheme."
    value_aliases:
      aadhaar:
        - uidai
        - aadhaar uid
        - uid aadhaar
        - unique identity scheme or aadhaar
        - unique identity uid scheme or aadhaar
  - name: scheme_status
    kind: enum
    description: "Operational maturity of the foundational ID scheme."
    identity_qualifiers: [basis]
    required_qualifiers: [basis]
    qualifier_enums:
      basis: [de_jure, de_facto]
    value_enum: [announced, piloting, operational, mandatory]
  - name: id_coverage_pct
    kind: percentage
    description: "Share of the population holding the foundational ID."
    identity_qualifiers: [population_basis, coverage_kind, measured_modeled]
    required_qualifiers: [population_basis]
    qualifier_enums:
      population_basis: [adults_15plus, total_pop, births, registered_holders]
      coverage_kind: [enrolled, issued, active]
      measured_modeled: [measured, modeled]
  - name: biometric_capture
    kind: enum
    description: "Biometric modality captured at enrolment."
    value_enum: [none, photo, fingerprint, iris, multi]
  - name: data_protection_law
    kind: boolean
    description: "Whether a data-protection law applies to the ID system."
    identity_qualifiers: [jurisdiction, stage, scope]
    required_qualifiers: [stage]
    qualifier_enums:
      stage: [enacted, in_force]
      scope: [comprehensive, sectoral]
  - name: legal_basis
    kind: name_year
    description: "Name + year of the statute/regulation establishing the scheme."
    identity_qualifiers: [jurisdiction]
```

- [ ] **Step 2: Write the failing golden round-trip test**

Create `tests/test_factbase_profile_roundtrip.py`:

```python
from open_deep_research.factbase import profile
from open_deep_research.factbase.profiles import country_digital_identity as py_mod


def _as_tuple(pd):
    return (
        pd.name, pd.value_kind, sorted(pd.identity_qualifiers), sorted(pd.required_qualifiers),
        {k: sorted(v) for k, v in pd.qualifier_enums.items()},
        None if pd.value_enum is None else sorted(pd.value_enum),
        pd.trust_threshold,
        {k: sorted(v) for k, v in pd.value_aliases.items()},
    )


def test_yaml_profile_matches_python_profile():
    py = py_mod.PROFILE
    yaml_prof = profile.load("country_digital_identity")
    assert yaml_prof.entity_type == py.entity_type
    assert {_as_tuple(p) for p in yaml_prof.properties} == {_as_tuple(p) for p in py.properties}


def test_yaml_profile_preserves_aadhaar_aliases():
    cov = profile.load("country_digital_identity").property("foundational_id_scheme")
    assert cov.aliases_for("uidai") == "aadhaar"
    assert cov.aliases_for("aadhaar uid") == "aadhaar"
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/test_factbase_profile_roundtrip.py -q`
Expected: FAIL — `load()` still imports the `.py` module, so `value_enum`/aliases parity holds by accident BUT the YAML is not yet read; to be certain the test exercises YAML, it fails first because `load` ignores the new file. (If it passes here because the `.py` is identical, Step 5 still re-verifies after the loader switch.)

- [ ] **Step 4: Switch `load()` to read YAML**

Replace `load` in `src/open_deep_research/factbase/profile.py` (lines 59-61):

```python
def load(name: str) -> Profile:
    """Load a domain profile from its YAML data file (validated on load)."""
    import yaml
    from importlib.resources import files

    from .profile_schema import profile_from_dict

    text = (
        files("open_deep_research.factbase.profiles")
        .joinpath(f"{name}.yaml")
        .read_text(encoding="utf-8")
    )
    return profile_from_dict(yaml.safe_load(text))
```

Remove the now-unused `import importlib` at the top of `profile.py` (line 3).

- [ ] **Step 5: Run the round-trip + existing profile tests**

Run: `uv run pytest tests/test_factbase_profile_roundtrip.py tests/test_factbase_profile.py -q`
Expected: PASS (5 passed) — YAML profile is identical to the `.py` one and the legacy tests still pass through the new loader.

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/factbase/profile.py \
        src/open_deep_research/factbase/profiles/country_digital_identity.yaml \
        tests/test_factbase_profile_roundtrip.py
git commit -m "feat(factbase): load() reads validated YAML; golden round-trip vs .py"
```

---

## Task 4: Registry meta-schema + YAML-reading `SourceRegistry.load()`

**Files:**
- Create: `src/open_deep_research/factbase/registry_schema.py`
- Modify: `src/open_deep_research/factbase/registry.py:8-11` (`load` only)
- Create: `src/open_deep_research/factbase/profiles/di_source_registry.yaml`
- Test: `tests/test_factbase_registry_yaml.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_factbase_registry_yaml.py`:

```python
import pytest

from open_deep_research.factbase.registry import SourceRegistry
from open_deep_research.factbase.registry_schema import registry_from_dict


def test_yaml_registry_matches_python_registry():
    from open_deep_research.factbase.profiles import di_source_registry as py_mod
    reg = SourceRegistry.load("di_source_registry")
    assert reg.tier("https://uidai.gov.in/x") == py_mod.ENTRIES["uidai.gov.in"]["tier"]
    assert reg.flags("https://id4d.worldbank.org/y") == ["modeled"]
    assert reg.meets_bar("https://gsma.com/z", "reputable") is True
    assert reg.tier("https://unknown.example/q") == "unvetted"


def test_invalid_tier_rejected():
    with pytest.raises(ValueError, match="tier"):
        registry_from_dict({"version": "1", "sources": [{"domain": "x.com", "tier": "gold"}]})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_factbase_registry_yaml.py -q`
Expected: FAIL — `ModuleNotFoundError: ...registry_schema`.

- [ ] **Step 3: Implement the registry meta-schema**

Create `src/open_deep_research/factbase/registry_schema.py`:

```python
"""Pydantic meta-schema for the source registry; builds the entries dict."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class _SourceEntry(BaseModel):
    domain: str
    tier: Literal["unvetted", "reputable", "authoritative"]
    flags: list[str] = []


class RegistryModel(BaseModel):
    version: str = "1"
    sources: list[_SourceEntry]


def registry_from_dict(data: dict) -> dict[str, dict]:
    """Validate a parsed registry dict and return the ``{domain: {tier, flags}}`` map."""
    model = RegistryModel.model_validate(data)
    return {s.domain: {"tier": s.tier, "flags": list(s.flags)} for s in model.sources}
```

- [ ] **Step 4: Author the registry YAML**

Create `src/open_deep_research/factbase/profiles/di_source_registry.yaml`:

```yaml
version: "1"
sources:
  - {domain: id4d.worldbank.org, tier: authoritative, flags: [modeled]}
  - {domain: worldbank.org, tier: authoritative, flags: [modeled]}
  - {domain: gsma.com, tier: authoritative}
  - {domain: mosip.io, tier: reputable}
  - {domain: uidai.gov.in, tier: reputable, flags: [incentivized]}
```

- [ ] **Step 5: Switch `SourceRegistry.load()` to read YAML**

Replace the `load` classmethod in `src/open_deep_research/factbase/registry.py` (lines 8-11):

```python
    @classmethod
    def load(cls, name: str) -> "SourceRegistry":
        import yaml
        from importlib.resources import files
        from .registry_schema import registry_from_dict
        text = (
            files("open_deep_research.factbase.profiles")
            .joinpath(f"{name}.yaml")
            .read_text(encoding="utf-8")
        )
        return cls(registry_from_dict(yaml.safe_load(text)))
```

Remove the now-unused `import importlib` at the top of `registry.py` (line 2).

- [ ] **Step 6: Run the registry tests**

Run: `uv run pytest tests/test_factbase_registry_yaml.py tests/test_factbase_registry.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/open_deep_research/factbase/registry_schema.py \
        src/open_deep_research/factbase/registry.py \
        src/open_deep_research/factbase/profiles/di_source_registry.yaml \
        tests/test_factbase_registry_yaml.py
git commit -m "feat(factbase): SourceRegistry loads validated YAML"
```

---

## Task 5: `dossier validate` CLI subcommand

**Files:**
- Modify: `src/open_deep_research/factbase/dossier.py`
- Test: `tests/test_dossier_validate.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_dossier_validate.py`:

```python
from open_deep_research.factbase.dossier import validate_profiles


def test_validate_passes_on_real_profiles():
    report, ok = validate_profiles()
    assert ok is True
    assert "country_digital_identity" in report
    assert "di_source_registry" in report


def test_validate_fails_on_bad_profile(tmp_path):
    bad = tmp_path / "country_bad.yaml"
    bad.write_text("entity_type: country\nproperties:\n  - {name: x, kind: wat}\n", encoding="utf-8")
    report, ok = validate_profiles(extra_paths=[bad])
    assert ok is False
    assert "country_bad" in report and "unknown kind" in report
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_dossier_validate.py -q`
Expected: FAIL — `ImportError: cannot import name 'validate_profiles'`.

- [ ] **Step 3: Implement `validate_profiles()` + the subcommand**

In `src/open_deep_research/factbase/dossier.py`, add this function above `_parser()`:

```python
def validate_profiles(extra_paths=None) -> tuple[str, bool]:
    """Validate every shipped profile/registry YAML (plus any extra_paths). Returns (report, ok).

    A registry file is any whose top-level YAML is a dict with a 'sources' key; everything
    else is treated as a profile.
    """
    import yaml
    from importlib.resources import files as _files
    from .profile_schema import profile_from_dict
    from .registry_schema import registry_from_dict

    paths = []
    pkg = _files("open_deep_research.factbase.profiles")
    for entry in pkg.iterdir():
        if entry.name.endswith(".yaml") and not entry.name.endswith(".draft.yaml"):
            paths.append(entry)
    paths.extend(extra_paths or [])

    lines, ok = [], True
    for path in paths:
        name = getattr(path, "name", str(path))
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "sources" in data:
                registry_from_dict(data)
            else:
                profile_from_dict(data)
            lines.append(f"OK    {name}")
        except Exception as e:  # noqa: BLE001 - report-and-continue is the point
            ok = False
            lines.append(f"FAIL  {name}: {e}")
    return "\n".join(lines), ok
```

Then register the subcommand inside `_parser()` (after the `stats` parser, before `return parser`):

```python
    sub.add_parser("validate", help="Validate all profile/registry YAML files.")
```

And handle it at the **top** of `run()`, before the DB connection (validation needs no DB). Replace the body of `run()` so the first statements are:

```python
async def run(argv, db_path=None) -> str:
    args = _parser().parse_args(argv)
    if args.command == "validate":
        report, ok = validate_profiles()
        return report if ok else report + "\nINVALID"
    db_path = db_path or get_db_path(None)
    # ... unchanged from here ...
```

Finally make `main()` exit non-zero on failure:

```python
def main():
    import sys
    out = asyncio.run(run(sys.argv[1:]))
    print(out)
    if out.endswith("INVALID"):
        sys.exit(1)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_dossier_validate.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Verify the CLI exit code end-to-end**

Run: `uv run dossier validate; echo "exit=$?"`
Expected: prints `OK    country_digital_identity.yaml` and `OK    di_source_registry.yaml`, then `exit=0`.

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/factbase/dossier.py tests/test_dossier_validate.py
git commit -m "feat(dossier): add validate subcommand (lints profile/registry YAML)"
```

---

## Task 6: Wire `dossier validate` into CI

**Files:**
- Modify: `.github/workflows/tests.yml`

- [ ] **Step 1: Add a validation step**

In `.github/workflows/tests.yml`, after the `Run unit tests` step, add:

```yaml
      - name: Validate factbase profiles
        run: uv run dossier validate
```

- [ ] **Step 2: Verify locally that the command CI will run passes**

Run: `uv run dossier validate; echo "exit=$?"`
Expected: `exit=0`.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/tests.yml
git commit -m "ci: validate factbase profile YAML on every run"
```

---

## Task 7: Remove the `.py` profile/registry modules; prove parity is frozen

The `.py` modules are now dead except for the round-trip test. Freeze the expected values into the test, then delete the modules so YAML is the single source of truth.

**Files:**
- Modify: `tests/test_factbase_profile_roundtrip.py` (drop the `.py` import; assert against a frozen snapshot)
- Delete: `src/open_deep_research/factbase/profiles/country_digital_identity.py`, `src/open_deep_research/factbase/profiles/di_source_registry.py`
- Modify: `tests/test_factbase_registry_yaml.py` (drop the `.py` import)

- [ ] **Step 1: Replace the `.py`-dependent round-trip with a frozen snapshot**

Replace the whole body of `tests/test_factbase_profile_roundtrip.py`:

```python
from open_deep_research.factbase import profile

# Frozen expectation captured from the original Python profile at migration time.
EXPECTED = {
    "foundational_id_scheme": ("name", [], [], {}, None),
    "scheme_status": ("enum", ["basis"], ["basis"], {"basis": ["de_jure", "de_facto"]},
                      ["announced", "mandatory", "operational", "piloting"]),
    "id_coverage_pct": ("percentage",
                        ["coverage_kind", "measured_modeled", "population_basis"],
                        ["population_basis"],
                        {"coverage_kind": ["active", "enrolled", "issued"],
                         "measured_modeled": ["measured", "modeled"],
                         "population_basis": ["adults_15plus", "births", "registered_holders", "total_pop"]},
                        None),
    "biometric_capture": ("enum", [], [], {},
                          ["fingerprint", "iris", "multi", "none", "photo"]),
    "data_protection_law": ("boolean", ["jurisdiction", "scope", "stage"], ["stage"],
                            {"scope": ["comprehensive", "sectoral"], "stage": ["enacted", "in_force"]},
                            None),
    "legal_basis": ("name_year", ["jurisdiction"], [], {}, None),
}


def test_yaml_profile_matches_frozen_snapshot():
    prof = profile.load("country_digital_identity")
    assert prof.entity_type == "country"
    got = {}
    for pd in prof.properties:
        got[pd.name] = (
            pd.value_kind, sorted(pd.identity_qualifiers), sorted(pd.required_qualifiers),
            {k: sorted(v) for k, v in pd.qualifier_enums.items()},
            None if pd.value_enum is None else sorted(pd.value_enum),
        )
    assert got == EXPECTED


def test_yaml_profile_preserves_aadhaar_aliases():
    scheme = profile.load("country_digital_identity").property("foundational_id_scheme")
    assert scheme.aliases_for("uidai") == "aadhaar"
    assert scheme.aliases_for("aadhaar uid") == "aadhaar"
```

- [ ] **Step 2: Drop the `.py` import from the registry test**

In `tests/test_factbase_registry_yaml.py`, replace `test_yaml_registry_matches_python_registry` with a `.py`-free version:

```python
def test_yaml_registry_values():
    reg = SourceRegistry.load("di_source_registry")
    assert reg.tier("https://uidai.gov.in/x") == "reputable"
    assert reg.flags("https://id4d.worldbank.org/y") == ["modeled"]
    assert reg.meets_bar("https://gsma.com/z", "reputable") is True
    assert reg.tier("https://unknown.example/q") == "unvetted"
```

- [ ] **Step 3: Delete the `.py` modules**

Run:
```bash
git rm src/open_deep_research/factbase/profiles/country_digital_identity.py \
       src/open_deep_research/factbase/profiles/di_source_registry.py
```

- [ ] **Step 4: Run the full factbase suite + the new tests**

Run: `uv run pytest tests/ -q -k "factbase or dossier"`
Expected: PASS — all green, no import of the deleted modules.

- [ ] **Step 5: Confirm nothing else imports the deleted modules**

Run: `grep -rn "profiles.country_digital_identity\|profiles.di_source_registry\|profiles import country_digital_identity\|profiles import di_source_registry" src/ tests/ | grep -v "\.yaml"`
Expected: no output (empty). If any line prints, fix that import to use `profile.load(...)` / `SourceRegistry.load(...)` before committing.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(factbase): YAML profiles are the single source of truth (remove .py)"
```

---

## Task 8: Installed-wheel smoke test (package-data ships)

Proves the YAML files are present when installed from a built wheel (not just in the source tree).

**Files:**
- Test: `tests/test_factbase_packaging.py`

- [ ] **Step 1: Write the test**

Create `tests/test_factbase_packaging.py`:

```python
from importlib.resources import files


def test_profile_yaml_is_packaged():
    pkg = files("open_deep_research.factbase.profiles")
    assert pkg.joinpath("country_digital_identity.yaml").is_file()
    assert pkg.joinpath("di_source_registry.yaml").is_file()


def test_load_works_via_resources():
    from open_deep_research.factbase import profile
    from open_deep_research.factbase.registry import SourceRegistry
    assert profile.load("country_digital_identity").entity_type == "country"
    assert SourceRegistry.load("di_source_registry").tier("https://gsma.com") == "authoritative"
```

- [ ] **Step 2: Run it (source-tree)**

Run: `uv run pytest tests/test_factbase_packaging.py -q`
Expected: PASS.

- [ ] **Step 3: Build a wheel and confirm the YAML is inside it**

Run:
```bash
uv build --wheel 2>/dev/null && python -c "import zipfile,glob; z=zipfile.ZipFile(sorted(glob.glob('dist/*.whl'))[-1]); print([n for n in z.namelist() if n.endswith('.yaml')])"
```
Expected: lists `open_deep_research/factbase/profiles/country_digital_identity.yaml` and `di_source_registry.yaml`. If the list is empty, the `package-data` glob in Task 1 Step 2 is wrong — fix it and re-run.

- [ ] **Step 4: Clean the build artifact and commit**

```bash
rm -rf dist
git add tests/test_factbase_packaging.py
git commit -m "test(factbase): assert profile YAML ships in the wheel"
```

---

## Final verification

- [ ] **Run the entire suite:**

Run: `uv run pytest -q`
Expected: all green (the pre-existing 8 supervisor/KB tests + all factbase tests + the 4 new test files).

- [ ] **Run the CI gate command:**

Run: `uv run dossier validate; echo "exit=$?"`
Expected: two `OK` lines, `exit=0`.

- [ ] **Confirm zero graph-behavior change:** the graph still calls `profile.load("country_digital_identity")` / `SourceRegistry.load("di_source_registry")` literally (unchanged) — it now reads YAML, but the resulting `Profile`/registry are identical (proven by the frozen-snapshot test). Runtime `profile_name` selection is Plan 6a Phase 2.

---

## Self-review notes (author)

- **Spec coverage (Phase-1 slice):** externalize profiles+registry to YAML ✓ (T3,T4); `value_aliases` preserved + golden round-trip ✓ (T3,T7); Pydantic meta-schema ✓ (T2,T4); `dossier validate` + CI ✓ (T5,T6); packaging `*.yaml` + `pyyaml` + wheel smoke test ✓ (T1,T8). Deferred to Phase 2 (explicitly out of this plan): `profile_name`/`registry_name` selection, prompt compilation, `profile_hash`/stamping + mismatch detection, recompute. Deferred to 6b: structural rebuild + scaffolding.
- **Placeholder scan:** none — every step has concrete code/commands.
- **Type consistency:** `profile_from_dict` → `Profile`; `registry_from_dict` → `dict[str,dict]` consumed by `SourceRegistry(entries)`; `validate_profiles()` returns `(report, ok)` used identically in CLI and tests; loader uses `importlib.resources.files(...).joinpath(...).read_text(...)` consistently in `profile.py`, `registry.py`, and `dossier.validate_profiles`.
