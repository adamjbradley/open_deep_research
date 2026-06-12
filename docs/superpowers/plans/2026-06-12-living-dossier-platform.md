# Living Fact Base — Implementation Plan (Plan 1 of N: Foundation)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the pure, fully-unit-tested foundation of the Living Fact Base — the migration framework + SQLite schema, the fact-identity service, the conflict & promotion policies (pure functions), the Digital-Identity profile, and entity resolution — with no graph integration yet.

**Architecture:** Per `docs/superpowers/specs/2026-06-12-living-dossier-platform-architecture.md` (v6) §11 build sequence, steps 1–4. A new `src/open_deep_research/factbase/` package holds focused modules. The conflict/promotion logic is **pure** (no I/O) so it is unit-tested in isolation; identity (tuple_key/canonicalize/value-equality) has a single owner; the schema is applied through a versioned migration framework (replacing the ad-hoc `executescript` in `storage.py`).

**Tech Stack:** Python 3.11, stdlib `aiosqlite`, `pydantic` (already used), `pytest` (configured: `testpaths=["tests"]`, `python_files=["test_*.py"]`).

**Scope note:** This is Plan 1 (Foundation). It produces a working, importable, fully-tested core with **zero** changes to the LangGraph graph. Plan 2 (extraction + graph hook: `run_source`, `FactExtractor`, `extract_facts` node, ingestion `FactWriter`/`FactQuery`, `preallocate_run`/`finalize_research_run`), Plan 3 (the `dossier` CLI), and Plan 4 (instrumentation + recompute) follow per §11 steps 5–8.

**Data types used across tasks** (defined in Task 3 / Task 5, repeated here for reference):
- `Qualifiers = dict[str, str | None]` — qualifier name → value, or `None` (⇒ treated as `"unspecified"`).
- `Fact` (Task 5 dataclass): `fact_id:int | None, tuple_key:str, as_of:int | None, value:str, unit:str | None, source_meets_bar:bool, has_unspecified_required:bool, admission:str ("provisional"|"trusted"), lifecycle:str ("current"|"stale"|"superseded")`.
- Intents (Task 4): `Promote(fact_id)`, `Demote(fact_id)`, `OpenConflict(tuple_key, as_of, fact_ids)`, `AutoClose(tuple_key, as_of)`.

---

### Task 1: Migration framework

**Files:**
- Create: `src/open_deep_research/factbase/__init__.py` (empty)
- Create: `src/open_deep_research/factbase/migrations.py`
- Test: `tests/test_factbase_migrations.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_migrations.py
import aiosqlite
import pytest
from open_deep_research.factbase import migrations


@pytest.mark.asyncio
async def test_apply_runs_pending_migrations_once():
    async with aiosqlite.connect(":memory:") as conn:
        steps = [
            (1, "CREATE TABLE a (id INTEGER PRIMARY KEY);"),
            (2, "CREATE TABLE b (id INTEGER PRIMARY KEY);"),
        ]
        await migrations.apply(conn, steps)
        # Re-applying is a no-op (idempotent), not an error.
        await migrations.apply(conn, steps)

        cur = await conn.execute("SELECT version FROM schema_migrations ORDER BY version")
        rows = [r[0] for r in await cur.fetchall()]
        assert rows == [1, 2]

        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('a','b')"
        )
        assert {r[0] for r in await cur.fetchall()} == {"a", "b"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_factbase_migrations.py -v`
Expected: FAIL — `ModuleNotFoundError: open_deep_research.factbase.migrations`

- [ ] **Step 3: Write minimal implementation**

```python
# src/open_deep_research/factbase/migrations.py
"""Versioned SQLite migration framework for the fact base.

Replaces the ad-hoc executescript(_SCHEMA) in storage.py: ordered (version, sql)
steps are applied once each, tracked in schema_migrations, so new tables land
safely on a populated DB.
"""
from __future__ import annotations

import aiosqlite

_TRACKING = "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)"


async def _applied_versions(conn: aiosqlite.Connection) -> set[int]:
    cur = await conn.execute("SELECT version FROM schema_migrations")
    return {row[0] for row in await cur.fetchall()}


async def apply(conn: aiosqlite.Connection, steps: list[tuple[int, str]]) -> None:
    """Apply each (version, sql) step not yet recorded, in ascending version order.

    Each step + its tracking insert run in one transaction so a failure rolls back.
    """
    await conn.execute(_TRACKING)
    await conn.commit()
    done = await _applied_versions(conn)
    for version, sql in sorted(steps, key=lambda s: s[0]):
        if version in done:
            continue
        try:
            await conn.executescript(sql)
            await conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, datetime('now'))",
                (version,),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_factbase_migrations.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/__init__.py src/open_deep_research/factbase/migrations.py tests/test_factbase_migrations.py
git commit -m "feat(factbase): versioned SQLite migration framework"
```

---

### Task 2: Fact-base schema migration

**Files:**
- Create: `src/open_deep_research/factbase/schema.py`
- Test: `tests/test_factbase_schema.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_schema.py
import aiosqlite
import pytest
from open_deep_research.factbase import migrations, schema


@pytest.mark.asyncio
async def test_schema_creates_all_factbase_tables():
    async with aiosqlite.connect(":memory:") as conn:
        await migrations.apply(conn, schema.STEPS)
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in await cur.fetchall()}
        assert {
            "run_source", "entity_type", "entity_instance", "unresolved_instance",
            "property_def", "source", "fact", "evidence", "fact_revision",
            "conflict", "conflict_member",
        } <= tables


@pytest.mark.asyncio
async def test_evidence_references_run_source_by_fk():
    async with aiosqlite.connect(":memory:") as conn:
        await migrations.apply(conn, schema.STEPS)
        cur = await conn.execute("PRAGMA foreign_key_list(evidence)")
        fks = await cur.fetchall()
        assert any(row[2] == "run_source" for row in fks)  # row[2] = referenced table
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_factbase_schema.py -v`
Expected: FAIL — `ModuleNotFoundError: open_deep_research.factbase.schema`

- [ ] **Step 3: Write minimal implementation**

```python
# src/open_deep_research/factbase/schema.py
"""Fact-base schema as ordered migration steps (consumed by migrations.apply).

Per Architecture v6 §7. `as_of` is the version axis (NOT in tuple_key); run_source
records EVERY encountered source with capture_status; evidence FK-references run_source.
"""
from __future__ import annotations

STEPS: list[tuple[int, str]] = [
    (1, """
    CREATE TABLE IF NOT EXISTS run_source (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id        INTEGER,
        source_url    TEXT,
        capture_status TEXT CHECK (capture_status IN ('raw_text','summarized','skipped')),
        text          TEXT,                 -- NULL for non-raw-text adapters
        content_hash  TEXT,
        retrieved_at  TEXT,
        soft_deleted_at TEXT
    );
    CREATE TABLE IF NOT EXISTS entity_type (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, profile_json TEXT
    );
    CREATE TABLE IF NOT EXISTS entity_instance (
        id INTEGER PRIMARY KEY AUTOINCREMENT, type_id INTEGER REFERENCES entity_type(id),
        canonical_key TEXT, name TEXT, aliases_json TEXT,
        UNIQUE(type_id, canonical_key)
    );
    CREATE TABLE IF NOT EXISTS unresolved_instance (
        id INTEGER PRIMARY KEY AUTOINCREMENT, type_id INTEGER, raw_name TEXT,
        run_id INTEGER, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS property_def (
        id INTEGER PRIMARY KEY AUTOINCREMENT, type_id INTEGER REFERENCES entity_type(id),
        name TEXT, value_kind TEXT, identity_qualifiers_json TEXT, validation_json TEXT,
        trust_threshold TEXT, UNIQUE(type_id, name)
    );
    CREATE TABLE IF NOT EXISTS source (
        id INTEGER PRIMARY KEY AUTOINCREMENT, url_or_domain TEXT,
        registry_version INTEGER, tier TEXT, flags_json TEXT, soft_deleted_at TEXT
    );
    CREATE TABLE IF NOT EXISTS fact (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instance_id INTEGER REFERENCES entity_instance(id),
        property_id INTEGER REFERENCES property_def(id),
        tuple_key   TEXT,            -- hash(instance, property, sorted non-temporal qualifiers)
        qualifiers_json TEXT,
        as_of       INTEGER,         -- version axis (year); NULL = unknown
        value       TEXT, unit TEXT,
        source_id   INTEGER REFERENCES source(id),
        admission   TEXT CHECK (admission IN ('provisional','trusted')),
        lifecycle   TEXT CHECK (lifecycle IN ('current','stale','superseded')),
        confidence  REAL,
        run_id      INTEGER,
        soft_deleted_at TEXT,
        created_at  TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_fact_tuple ON fact(tuple_key, as_of);
    CREATE TABLE IF NOT EXISTS evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fact_id INTEGER REFERENCES fact(id),
        quoted_span TEXT,
        run_source_id INTEGER REFERENCES run_source(id),   -- stable FK, NOT a url string
        doc_identity TEXT, retrieved_at TEXT
    );
    CREATE TABLE IF NOT EXISTS fact_revision (
        id INTEGER PRIMARY KEY AUTOINCREMENT, fact_id INTEGER REFERENCES fact(id),
        change TEXT, cause TEXT, why TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS conflict (
        id INTEGER PRIMARY KEY AUTOINCREMENT, tuple_key TEXT, as_of INTEGER,
        status TEXT CHECK (status IN ('open','resolved')), created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS conflict_member (
        conflict_id INTEGER REFERENCES conflict(id), fact_id INTEGER REFERENCES fact(id)
    );
    """),
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_factbase_schema.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/schema.py tests/test_factbase_schema.py
git commit -m "feat(factbase): fact-base schema migration (run_source, fact, evidence FK, ...)"
```

---

### Task 3: FactIdentity — canonicalize + value equality

**Files:**
- Create: `src/open_deep_research/factbase/identity.py`
- Test: `tests/test_factbase_identity.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_identity.py
from open_deep_research.factbase import identity


def test_canonicalize_normalizes_whitespace_and_case_within_same_unit():
    assert identity.canonicalize("  99 ", "%") == identity.canonicalize("99", "%")
    assert identity.canonicalize("Aadhaar", None) == identity.canonicalize("aadhaar", None)


def test_values_equal_true_for_same_normalized_value_and_unit():
    assert identity.values_equal("99", "%", "99", "%") is True


def test_values_equal_false_for_different_value():
    assert identity.values_equal("99", "%", "87", "%") is False


def test_values_equal_false_for_different_unit_no_normalization_in_v1():
    # v1 does NOT unit-convert: same magnitude, different unit => not equal (flagged uncomparable upstream)
    assert identity.values_equal("5", "mg/L", "5", "umol/L") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_factbase_identity.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# src/open_deep_research/factbase/identity.py
"""Single owner of fact identity: value canonicalization + equality + tuple_key.

v1 canonicalization is whitespace/case normalization within an IDENTICAL unit only.
Unit conversion and numeric tolerance are deferred (Architecture §12) — non-identical
units are NOT equal here; callers flag them 'uncomparable'.
"""
from __future__ import annotations

import hashlib
import re

_WS = re.compile(r"\s+")


def canonicalize(value: str, unit: str | None) -> str:
    v = _WS.sub(" ", (value or "").strip().lower())
    u = _WS.sub(" ", (unit or "").strip().lower())
    return f"{v}␟{u}"  # unit symbol separator keeps value/unit distinct


def values_equal(a_value: str, a_unit: str | None, b_value: str, b_unit: str | None) -> bool:
    return canonicalize(a_value, a_unit) == canonicalize(b_value, b_unit)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_factbase_identity.py -v`
Expected: PASS (all 4)

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/identity.py tests/test_factbase_identity.py
git commit -m "feat(factbase): FactIdentity canonicalize + value-equality (v1 exact-match)"
```

---

### Task 4: FactIdentity — tuple_key (qualifier-aware, as_of excluded)

**Files:**
- Modify: `src/open_deep_research/factbase/identity.py`
- Test: `tests/test_factbase_identity.py` (add to existing file)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_factbase_identity.py
def test_tuple_key_excludes_as_of_and_orders_qualifiers():
    # as_of is the version axis: it must NOT change the tuple_key.
    k1 = identity.tuple_key(7, "id_coverage_pct", {"population_basis": "adults_15plus", "coverage_kind": "enrolled"})
    k2 = identity.tuple_key(7, "id_coverage_pct", {"coverage_kind": "enrolled", "population_basis": "adults_15plus"})
    assert k1 == k2  # qualifier order does not matter


def test_tuple_key_differs_by_qualifier_value():
    k1 = identity.tuple_key(7, "id_coverage_pct", {"population_basis": "adults_15plus"})
    k2 = identity.tuple_key(7, "id_coverage_pct", {"population_basis": "registered_holders"})
    assert k1 != k2  # different denominator => distinct fact, not a conflict


def test_tuple_key_unspecified_qualifier_is_its_own_tuple():
    specified = identity.tuple_key(7, "id_coverage_pct", {"population_basis": "adults_15plus"})
    unspec = identity.tuple_key(7, "id_coverage_pct", {"population_basis": None})
    assert specified != unspec  # 'unspecified' never groups with a specified value
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_factbase_identity.py -k tuple_key -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'tuple_key'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to src/open_deep_research/factbase/identity.py
def tuple_key(instance_id: int, property_name: str, qualifiers: dict[str, str | None]) -> str:
    """Hash of (instance, property, sorted non-temporal qualifiers).

    `as_of` is the version axis and is deliberately NOT a parameter here.
    A None qualifier value is rendered as the literal 'unspecified', so a fact whose
    required qualifier could not be extracted gets its own (non-promotable) tuple.
    """
    parts = [str(instance_id), property_name]
    for name in sorted(qualifiers):
        val = qualifiers[name]
        parts.append(f"{name}={'unspecified' if val is None else val.strip().lower()}")
    raw = "␞".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_factbase_identity.py -v`
Expected: PASS (all 7 in the file)

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/identity.py tests/test_factbase_identity.py
git commit -m "feat(factbase): tuple_key (qualifier-aware, as_of-excluded, unspecified-isolating)"
```

---

### Task 5: Fact dataclass + intents

**Files:**
- Create: `src/open_deep_research/factbase/model.py`
- Test: `tests/test_factbase_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_model.py
from open_deep_research.factbase import model


def test_fact_defaults_provisional_current():
    f = model.Fact(fact_id=1, tuple_key="t", as_of=2024, value="99", unit="%",
                   source_meets_bar=True, has_unspecified_required=False)
    assert f.admission == "provisional"
    assert f.lifecycle == "current"


def test_intents_carry_their_payload():
    assert model.Promote(5).fact_id == 5
    assert model.OpenConflict("t", 2024, [1, 2]).fact_ids == [1, 2]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_factbase_model.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# src/open_deep_research/factbase/model.py
"""Pure data types the policies operate on (no I/O)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Fact:
    fact_id: int | None
    tuple_key: str
    as_of: int | None
    value: str
    unit: str | None
    source_meets_bar: bool          # source tier >= property threshold (resolved upstream)
    has_unspecified_required: bool  # any required identity qualifier == 'unspecified'
    admission: str = "provisional"  # 'provisional' | 'trusted'
    lifecycle: str = "current"      # 'current' | 'stale' | 'superseded'


@dataclass
class Promote:
    fact_id: int


@dataclass
class Demote:
    fact_id: int


@dataclass
class OpenConflict:
    tuple_key: str
    as_of: int | None
    fact_ids: list[int] = field(default_factory=list)


@dataclass
class AutoClose:
    tuple_key: str
    as_of: int | None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_factbase_model.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/model.py tests/test_factbase_model.py
git commit -m "feat(factbase): Fact dataclass + intent types"
```

---

### Task 6: ConflictPolicy (pure)

**Files:**
- Create: `src/open_deep_research/factbase/conflict.py`
- Test: `tests/test_factbase_conflict.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_conflict.py
from open_deep_research.factbase import conflict, model


def _f(fid, value, as_of=2024, bar=True, tk="t"):
    return model.Fact(fact_id=fid, tuple_key=tk, as_of=as_of, value=value, unit="%",
                      source_meets_bar=bar, has_unspecified_required=False)


def test_two_trust_bar_values_same_bucket_open_conflict():
    intents = conflict.detect([_f(1, "99"), _f(2, "87")])
    opens = [i for i in intents if isinstance(i, model.OpenConflict)]
    assert len(opens) == 1
    assert sorted(opens[0].fact_ids) == [1, 2]


def test_same_value_no_conflict():
    assert conflict.detect([_f(1, "99"), _f(2, "99")]) == []


def test_different_as_of_is_not_a_conflict():
    # different year => different bucket => versions, not a disagreement
    assert conflict.detect([_f(1, "99", as_of=2023), _f(2, "87", as_of=2024)]) == []


def test_lower_tier_disagreement_does_not_open_conflict():
    assert conflict.detect([_f(1, "99", bar=True), _f(2, "87", bar=False)]) == []


def test_collapse_to_one_value_auto_closes():
    # only one distinct trust-bar value remains in a bucket that had an open conflict
    intents = conflict.detect([_f(1, "99"), _f(2, "99")], had_open_conflict=True)
    assert any(isinstance(i, model.AutoClose) for i in intents)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_factbase_conflict.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# src/open_deep_research/factbase/conflict.py
"""Pure conflict detection. Operates on facts already grouped into ONE
(tuple_key, as_of) bucket. Returns intents; never touches storage."""
from __future__ import annotations

from . import identity, model


def detect(bucket: list[model.Fact], had_open_conflict: bool = False) -> list[model.Intent]:
    trust_bar = [f for f in bucket if f.source_meets_bar]
    distinct = {identity.canonicalize(f.value, f.unit) for f in trust_bar}
    intents: list = []
    if len(distinct) >= 2:
        intents.append(
            model.OpenConflict(
                tuple_key=bucket[0].tuple_key,
                as_of=bucket[0].as_of,
                fact_ids=sorted(f.fact_id for f in trust_bar if f.fact_id is not None),
            )
        )
    elif had_open_conflict and len(distinct) <= 1:
        intents.append(model.AutoClose(tuple_key=bucket[0].tuple_key, as_of=bucket[0].as_of))
    return intents
```

Also add the `Intent` union alias to `model.py` (append):

```python
# append to src/open_deep_research/factbase/model.py
Intent = Promote | Demote | OpenConflict | AutoClose
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_factbase_conflict.py -v`
Expected: PASS (all 5)

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/conflict.py src/open_deep_research/factbase/model.py tests/test_factbase_conflict.py
git commit -m "feat(factbase): pure ConflictPolicy (same-qualifier+as_of, trust-bar, auto-close)"
```

---

### Task 7: PromotionPolicy (pure)

**Files:**
- Create: `src/open_deep_research/factbase/promotion.py`
- Test: `tests/test_factbase_promotion.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_promotion.py
from open_deep_research.factbase import promotion, model


def _f(fid, value, bar=True, unspec=False):
    return model.Fact(fact_id=fid, tuple_key="t", as_of=2024, value=value, unit="%",
                      source_meets_bar=bar, has_unspecified_required=unspec)


def test_promote_when_bar_met_no_conflict_no_unspecified():
    f = _f(1, "99")
    assert promotion.evaluate(f, bucket=[f], has_open_conflict=False) == model.Promote(1)


def test_no_promote_when_below_bar():
    f = _f(1, "99", bar=False)
    assert promotion.evaluate(f, bucket=[f], has_open_conflict=False) is None


def test_no_promote_when_unspecified_qualifier():
    f = _f(1, "99", unspec=True)
    assert promotion.evaluate(f, bucket=[f], has_open_conflict=False) is None


def test_no_promote_when_open_conflict_in_bucket():
    f = _f(1, "99")
    assert promotion.evaluate(f, bucket=[f, _f(2, "87")], has_open_conflict=True) is None


def test_trusted_fact_demoted_when_conflict_opens():
    f = _f(1, "99")
    f.admission = "trusted"
    assert promotion.evaluate(f, bucket=[f, _f(2, "87")], has_open_conflict=True) == model.Demote(1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_factbase_promotion.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# src/open_deep_research/factbase/promotion.py
"""Pure promotion/demotion decision for one fact within its (tuple_key, as_of) bucket."""
from __future__ import annotations

from . import model


def evaluate(fact: model.Fact, bucket: list[model.Fact], has_open_conflict: bool):
    """Return a Promote/Demote intent, or None to leave admission unchanged.

    Promote iff: source meets the property trust bar AND no unspecified required
    qualifier AND no open conflict in the bucket. A trusted fact that now sits in a
    conflicted bucket is demoted. (Per Architecture §6.)
    """
    eligible = (
        fact.source_meets_bar
        and not fact.has_unspecified_required
        and not has_open_conflict
    )
    if eligible and fact.admission != "trusted":
        return model.Promote(fact.fact_id)
    if not eligible and fact.admission == "trusted":
        return model.Demote(fact.fact_id)
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_factbase_promotion.py -v`
Expected: PASS (all 5)

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/promotion.py tests/test_factbase_promotion.py
git commit -m "feat(factbase): pure PromotionPolicy (bar + no-unspecified + no-conflict)"
```

---

### Task 8: Digital-Identity profile (property defs + qualifiers + validation)

**Files:**
- Create: `src/open_deep_research/factbase/profiles/country_digital_identity.py`
- Create: `src/open_deep_research/factbase/profile.py`
- Test: `tests/test_factbase_profile.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_profile.py
from open_deep_research.factbase import profile


def test_di_profile_has_expected_properties_with_qualifiers():
    p = profile.load("country_digital_identity")
    names = {pd.name for pd in p.properties}
    assert {"foundational_id_scheme", "scheme_status", "id_coverage_pct",
            "biometric_capture", "data_protection_law", "legal_basis"} <= names

    cov = p.property("id_coverage_pct")
    assert set(cov.identity_qualifiers) == {"population_basis", "coverage_kind", "measured_modeled"}
    assert "registered_holders" in cov.qualifier_enums["population_basis"]


def test_validation_rejects_out_of_range_percentage():
    cov = profile.load("country_digital_identity").property("id_coverage_pct")
    assert cov.validate("412") is False
    assert cov.validate("87") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_factbase_profile.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# src/open_deep_research/factbase/profile.py
"""Domain profile: entity types, their property defs, qualifiers, validation."""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field


@dataclass
class PropertyDef:
    name: str
    value_kind: str                       # 'percentage'|'enum'|'boolean'|'name'|'name_year'
    identity_qualifiers: list[str] = field(default_factory=list)
    qualifier_enums: dict[str, list[str]] = field(default_factory=dict)
    value_enum: list[str] | None = None   # for value_kind == 'enum'
    trust_threshold: str = "reputable"    # min registry tier to promote

    def validate(self, value: str) -> bool:
        v = (value or "").strip()
        if self.value_kind == "percentage":
            try:
                return 0.0 <= float(v.rstrip("%")) <= 100.0
            except ValueError:
                return False
        if self.value_kind == "enum" and self.value_enum is not None:
            return v.lower() in {e.lower() for e in self.value_enum}
        return bool(v)


@dataclass
class Profile:
    entity_type: str
    properties: list[PropertyDef]

    def property(self, name: str) -> PropertyDef:
        for pd in self.properties:
            if pd.name == name:
                return pd
        raise KeyError(name)


def load(name: str) -> Profile:
    mod = importlib.import_module(f"open_deep_research.factbase.profiles.{name}")
    return mod.PROFILE
```

```python
# src/open_deep_research/factbase/profiles/country_digital_identity.py
"""Digital Identity pillar profile for the `country` entity type (Feature Spec §2.1)."""
from __future__ import annotations

from open_deep_research.factbase.profile import Profile, PropertyDef

PROFILE = Profile(
    entity_type="country",
    properties=[
        PropertyDef("foundational_id_scheme", "name"),
        PropertyDef("scheme_status", "enum",
                    identity_qualifiers=["basis"],
                    qualifier_enums={"basis": ["de_jure", "de_facto"]},
                    value_enum=["announced", "piloting", "operational", "mandatory"]),
        PropertyDef("id_coverage_pct", "percentage",
                    identity_qualifiers=["population_basis", "coverage_kind", "measured_modeled"],
                    qualifier_enums={
                        "population_basis": ["adults_15plus", "total_pop", "births", "registered_holders"],
                        "coverage_kind": ["enrolled", "issued", "active"],
                        "measured_modeled": ["measured", "modeled"],
                    }),
        PropertyDef("biometric_capture", "enum",
                    value_enum=["none", "photo", "fingerprint", "iris", "multi"]),
        PropertyDef("data_protection_law", "boolean",
                    identity_qualifiers=["jurisdiction", "stage", "scope"],
                    qualifier_enums={
                        "stage": ["enacted", "in_force"],
                        "scope": ["comprehensive", "sectoral"],
                    }),
        PropertyDef("legal_basis", "name_year", identity_qualifiers=["jurisdiction"]),
    ],
)
```

(Create `src/open_deep_research/factbase/profiles/__init__.py` empty.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_factbase_profile.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/profile.py src/open_deep_research/factbase/profiles/
git add tests/test_factbase_profile.py
git commit -m "feat(factbase): Digital-Identity domain profile + property validation"
```

---

### Task 9: EntityResolver (ISO-3166 + alias; miss → unresolved)

**Files:**
- Create: `src/open_deep_research/factbase/entities.py`
- Test: `tests/test_factbase_entities.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_entities.py
from open_deep_research.factbase import entities


def test_resolves_canonical_and_common_aliases():
    r = entities.CountryResolver()
    assert r.resolve("France") == "FRA"
    assert r.resolve("Türkiye") == "TUR"
    assert r.resolve("Turkey") == "TUR"
    assert r.resolve("Côte d'Ivoire") == "CIV"


def test_miss_returns_none_never_guesses():
    r = entities.CountryResolver()
    assert r.resolve("Atlantis") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_factbase_entities.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# src/open_deep_research/factbase/entities.py
"""Entity-instance resolution. Country resolver maps names/aliases -> ISO-3166 alpha-3.

A miss returns None; the caller quarantines it in `unresolved_instance` and NEVER
auto-creates a canonical entity (Architecture §3/§5).
"""
from __future__ import annotations

import re

_NORM = re.compile(r"[^a-z0-9]+")


def _norm(s: str) -> str:
    return _NORM.sub("", (s or "").lower())


# Seed map; full ISO-3166 + alias manifest loaded from data in a later task (§12).
_ALPHA3: dict[str, str] = {
    "france": "FRA",
    "turkiye": "TUR", "turkey": "TUR",
    "cotedivoire": "CIV", "ivorycoast": "CIV",
    "india": "IND", "estonia": "EST", "singapore": "SGP", "nigeria": "NGA",
    "kenya": "KEN", "brazil": "BRA", "indonesia": "IDN", "pakistan": "PAK",
    "philippines": "PHL", "ukraine": "UKR", "rwanda": "RWA", "peru": "PER",
    "bangladesh": "BGD", "ethiopia": "ETH", "morocco": "MAR", "mexico": "MEX",
}


class CountryResolver:
    def resolve(self, name: str) -> str | None:
        return _ALPHA3.get(_norm(name))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_factbase_entities.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/entities.py tests/test_factbase_entities.py
git commit -m "feat(factbase): CountryResolver (ISO-3166 alpha-3 + aliases; miss=None)"
```

---

### Task 10: Foundation smoke test (the pieces compose)

**Files:**
- Test: `tests/test_factbase_foundation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_foundation.py
"""End-to-end (pure) walk: resolve -> tuple_key -> validate -> conflict -> promotion."""
from open_deep_research.factbase import entities, identity, profile, conflict, promotion, model


def test_two_sources_same_qualifiers_conflict_blocks_promotion():
    r = entities.CountryResolver()
    iid = r.resolve("India")  # 'IND'
    cov = profile.load("country_digital_identity").property("id_coverage_pct")
    quals = {"population_basis": "adults_15plus", "coverage_kind": "enrolled", "measured_modeled": "measured"}
    tk = identity.tuple_key(hash(iid) & 0xffff, cov.name, quals)

    assert cov.validate("99") and cov.validate("87")
    facts = [
        model.Fact(1, tk, 2024, "99", "%", source_meets_bar=True, has_unspecified_required=False),
        model.Fact(2, tk, 2024, "87", "%", source_meets_bar=True, has_unspecified_required=False),
    ]
    conflicts = conflict.detect(facts)
    assert any(isinstance(i, model.OpenConflict) for i in conflicts)
    # neither promotes while the conflict is open
    assert promotion.evaluate(facts[0], facts, has_open_conflict=True) is None


def test_different_denominator_is_not_a_conflict():
    cov = "id_coverage_pct"
    tk_a = identity.tuple_key(1, cov, {"population_basis": "adults_15plus"})
    tk_b = identity.tuple_key(1, cov, {"population_basis": "registered_holders"})
    a = model.Fact(1, tk_a, 2024, "99", "%", True, False)
    b = model.Fact(2, tk_b, 2024, "60", "%", True, False)
    # different tuple_key => never in the same bucket => never a conflict
    assert conflict.detect([a]) == [] and conflict.detect([b]) == []
    assert tk_a != tk_b
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_factbase_foundation.py -v`
Expected: FAIL until all prior tasks are in (it imports every module)

- [ ] **Step 3: (no new code — this is an integration check of existing modules)**

If it fails for reasons other than assertions, fix the offending module from its own task.

- [ ] **Step 4: Run the full fact-base suite**

Run: `pytest tests/test_factbase_*.py -v`
Expected: PASS (all foundation tests)

- [ ] **Step 5: Commit**

```bash
git add tests/test_factbase_foundation.py
git commit -m "test(factbase): foundation composition smoke test (conflict blocks promotion)"
```

---

## Foundation complete

After Task 10, the fact-base **core** is implemented and fully unit-tested with no graph changes:
migration framework + schema, `FactIdentity` (canonicalize/equality/tuple_key), pure `ConflictPolicy`
and `PromotionPolicy`, the Digital-Identity profile + validation, and `CountryResolver`.

**Next plans (per Architecture §11 steps 5–8):**
- **Plan 2 — Extraction & graph hook:** `RunSourceStore` (written at the tool layer, utils.py search
  adapters), `FactExtractor` (one call per source, flat records, abstain, span-verification against
  `run_source.text`), the `preallocate_run` node + `finalize_research_run` UPDATE port, the
  `extract_facts` node, and the ingestion application service + `FactWriter` (atomic tx) / `FactQuery`.
- **Plan 3 — `dossier` CLI:** `dossier show` / `compare` / `--format csv|md`, one canonical render path.
- **Plan 4 — Instrumentation & recompute:** metrics (coverage, groundedness, false-conflict + drop-rate),
  the registry-version recompute pass, the stale-`running` reaper.
