# Profiles as Data — Plan 6b-2 (Structural Rebuild) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans.

**Goal:** After a profile's *structural* fields change (identity_qualifiers, value_enum, required_qualifiers, trust_threshold, or a property rename/remove), re-derive every stored fact's `tuple_key`, then re-run conflict grouping **and** promotion/demotion from the retained `fact`/`evidence` rows — no re-extraction. Exposed as `dossier recompute --rebuild`.

**Architecture:** A new `factbase/rebuild.py:rebuild_structural(conn, profile, registry, *, rename, on_removed)` that reconstructs `model.Fact` objects from stored rows (recomputing `source_meets_bar` via the registry and `has_unspecified_required` from `required_qualifiers` — neither is a stored column), recomputes `tuple_key` via `identity.tuple_key`, clears open conflicts, then **reuses the existing `conflict.detect` + `promotion.evaluate`** over the regrouped buckets. Unlike ingest (promote-only), the rebuild also **demotes** facts that became ineligible.

**Tech Stack:** Python 3.11, `aiosqlite`, pytest. Reuses `identity`/`conflict`/`promotion`/`model`/`recompute`. No new deps.

**Builds on:** Phases 1-4 + 6b-1 (merged). Phase 4 added `dossier recompute` (normalization, `force=True`); this adds the `--rebuild` variant.

**⚠️ Correctness-critical:** a bug here silently corrupts the trust/conflict state (the factbase's core value). The rebuild must reuse the *exact same* `conflict.detect`/`promotion.evaluate` as ingest (not reimplement), and must handle demotion. Tests assert observable trust/conflict outcomes, not internals.

---

## Reference (verbatim, from the current code)

- `identity.tuple_key(instance_id, property_name, qualifiers)` — sha256 of `instance␞property␞<name=val sorted>`, `None`→`"unspecified"` (`identity.py:95-104`). Ingest builds `quals = {q: rec[...].get(q) for q in pd.identity_qualifiers}` (`ingest.py:42`).
- `conflict.detect(bucket, had_open_conflict=False)` — opens a conflict when, within an `as_of` group, ≥2 trust-bar facts have distinct canonical values; emits `OpenConflict(tuple_key, as_of, fact_ids)` (`conflict.py:6-46`).
- `promotion.evaluate(fact, bucket, has_open_conflict)` → `Promote`/`Demote`/None: trusted iff `source_meets_bar and not has_unspecified_required and not has_open_conflict` (`promotion.py:6-12`).
- `model.Fact(fact_id, tuple_key, as_of, value, unit, source_meets_bar, has_unspecified_required, admission='provisional', lifecycle='current', canonical_value, canonical_unit)` (`model.py`).
- `registry.meets_bar(url, threshold)` with `threshold = pd.trust_threshold` (default "reputable").
- `fact` columns: `id, instance_key, property_name, qualifiers_json, as_of, value, unit, canonical_value, canonical_unit, source_id, admission, soft_deleted_at` (+ `tuple_key`). `source(id, url_or_domain, tier, flags_json)`. `conflict(id, tuple_key, as_of, status open|resolved)`, `conflict_member(conflict_id, fact_id)`.

---

## File structure

| File | Responsibility | Action |
|---|---|---|
| `src/open_deep_research/factbase/rebuild.py` | `rebuild_structural(...)` | Create |
| `src/open_deep_research/factbase/dossier.py` | `recompute --rebuild [--rename] [--on-removed]` | Modify |
| `tests/test_factbase_rebuild.py` | tuple_key recompute → conflict opens; demotion; rename; orphan | Create |

---

## Task 1: `rebuild.py`

**Files:**
- Create: `src/open_deep_research/factbase/rebuild.py`
- Test: `tests/test_factbase_rebuild.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_factbase_rebuild.py`:

```python
import asyncio
import json

import aiosqlite

from open_deep_research.factbase import migrations, schema
from open_deep_research.factbase.profile_schema import profile_from_dict
from open_deep_research.factbase.registry_schema import registry_from_dict
from open_deep_research.factbase.registry import SourceRegistry
from open_deep_research.factbase.rebuild import rebuild_structural

REG = SourceRegistry(registry_from_dict({"version": "1", "sources": [
    {"domain": "a.example", "tier": "authoritative"},
    {"domain": "b.example", "tier": "authoritative"},
]}))


async def _seed_source(conn, url):
    cur = await conn.execute(
        "INSERT INTO source (url_or_domain, tier, flags_json) VALUES (?,?,?)", (url, "authoritative", "[]"))
    return cur.lastrowid


async def _seed_fact(conn, *, property_name, quals, value, source_id, tuple_key, as_of=2024):
    await conn.execute(
        "INSERT INTO fact (tuple_key, instance_key, property_name, qualifiers_json, as_of, value, "
        "canonical_value, source_id, admission, lifecycle, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (tuple_key, "india", property_name, json.dumps(quals), as_of, value, value, source_id,
         "trusted", "current", "now"))


def test_dropping_identity_qualifier_collapses_tuples_and_opens_conflict(tmp_path):
    db = str(tmp_path / "fb.db")

    async def go():
        async with aiosqlite.connect(db) as conn:
            await migrations.apply(conn, schema.STEPS)
            sa = await _seed_source(conn, "https://a.example/x")
            sb = await _seed_source(conn, "https://b.example/y")
            # Under the OLD profile, 'basis' was an identity qualifier, so these two facts had
            # DIFFERENT tuple_keys (de_jure vs de_facto) and did not conflict. Seed them that way:
            await _seed_fact(conn, property_name="scheme_status", quals={"basis": "de_jure"},
                             value="operational", source_id=sa, tuple_key="OLD_TK_1")
            await _seed_fact(conn, property_name="scheme_status", quals={"basis": "de_facto"},
                             value="mandatory", source_id=sb, tuple_key="OLD_TK_2")
            await conn.commit()

            # NEW profile: 'basis' is no longer an identity qualifier -> both collapse to one
            # tuple_key with two distinct values -> a conflict must open, both demote.
            new_prof = profile_from_dict({"entity_type": "country", "version": "2", "properties": [
                {"name": "scheme_status", "kind": "enum",
                 "value_enum": ["operational", "mandatory", "announced", "piloting"]}]})

            stats = await rebuild_structural(conn, new_prof, REG)
            assert stats["tuple_keys_changed"] == 2
            assert stats["conflicts_opened"] == 1
            assert stats["demoted"] == 2  # both were 'trusted', now conflicted -> provisional

            cur = await conn.execute("SELECT COUNT(*) FROM conflict WHERE status='open'")
            assert (await cur.fetchone())[0] == 1
            cur = await conn.execute("SELECT DISTINCT tuple_key FROM fact WHERE soft_deleted_at IS NULL")
            assert len(await cur.fetchall()) == 1  # collapsed to one tuple
            cur = await conn.execute("SELECT COUNT(*) FROM fact WHERE admission='trusted'")
            assert (await cur.fetchone())[0] == 0  # both demoted

    asyncio.run(go())


def test_property_remove_orphan_policy(tmp_path):
    db = str(tmp_path / "fb.db")

    async def go():
        async with aiosqlite.connect(db) as conn:
            await migrations.apply(conn, schema.STEPS)
            s = await _seed_source(conn, "https://a.example/x")
            await _seed_fact(conn, property_name="gone_prop", quals={}, value="v",
                             source_id=s, tuple_key="TK")
            await conn.commit()
            prof = profile_from_dict({"entity_type": "country", "version": "1",
                                      "properties": [{"name": "kept", "kind": "name"}]})

            # retain (default): orphan fact stays, not soft-deleted
            stats = await rebuild_structural(conn, prof, REG, on_removed="retain")
            assert stats["orphaned"] == 1
            cur = await conn.execute("SELECT soft_deleted_at FROM fact WHERE property_name='gone_prop'")
            assert (await cur.fetchone())[0] is None

            # soft_delete: orphan fact is soft-deleted
            stats = await rebuild_structural(conn, prof, REG, on_removed="soft_delete")
            cur = await conn.execute("SELECT soft_deleted_at FROM fact WHERE property_name='gone_prop'")
            assert (await cur.fetchone())[0] is not None

    asyncio.run(go())


def test_rename_map_moves_facts_to_new_property(tmp_path):
    db = str(tmp_path / "fb.db")

    async def go():
        async with aiosqlite.connect(db) as conn:
            await migrations.apply(conn, schema.STEPS)
            s = await _seed_source(conn, "https://a.example/x")
            await _seed_fact(conn, property_name="old_name", quals={}, value="v",
                             source_id=s, tuple_key="TK")
            await conn.commit()
            prof = profile_from_dict({"entity_type": "country", "version": "1",
                                      "properties": [{"name": "new_name", "kind": "name"}]})
            stats = await rebuild_structural(conn, prof, REG, rename={"old_name": "new_name"})
            assert stats["orphaned"] == 0  # renamed, not orphaned
            cur = await conn.execute("SELECT property_name FROM fact WHERE id=1")
            assert (await cur.fetchone())[0] == "new_name"

    asyncio.run(go())
```

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_factbase_rebuild.py -q`
Expected: FAIL (`ModuleNotFoundError: ...rebuild`).

- [ ] **Step 3: Implement**

Create `src/open_deep_research/factbase/rebuild.py`:

```python
"""Structural rebuild: re-derive tuple_key, conflicts, and promotion for stored facts
after a profile's structural fields change (identity_qualifiers, value_enum,
required_qualifiers, trust_threshold, or a property rename/remove).

Reconstructs ``model.Fact`` objects from retained rows (recomputing source_meets_bar
and has_unspecified_required, which are not stored columns) and re-runs the SAME
``conflict.detect`` + ``promotion.evaluate`` as ingestion -- plus demotion, which
ingestion never does. Forward-only; preserves resolved (human-adjudicated) conflicts.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import aiosqlite

from . import conflict as _conflict, identity as _identity, model as _model, promotion as _promotion
from .recompute import backfill_canonical_values


async def rebuild_structural(conn: aiosqlite.Connection, profile, registry, *,
                             rename: dict | None = None, on_removed: str = "retain") -> dict:
    conn.row_factory = aiosqlite.Row
    now = datetime.now(timezone.utc).isoformat()
    stats = {"tuple_keys_changed": 0, "conflicts_opened": 0,
             "promoted": 0, "demoted": 0, "orphaned": 0}

    # 1) Apply property renames so renamed facts attach to the new property def.
    for old, new in (rename or {}).items():
        await conn.execute(
            "UPDATE fact SET property_name=? WHERE property_name=? AND soft_deleted_at IS NULL",
            (new, old))

    # 2) Re-derive canonical values (value_enum / alias / kind changes) so conflict compares right.
    await backfill_canonical_values(conn, profile, force=True)

    # 3) Reconstruct each fact; recompute tuple_key; handle orphans; bucket by (tuple_key, as_of).
    rows = await (await conn.execute(
        "SELECT id, instance_key, property_name, qualifiers_json, as_of, value, unit, "
        "canonical_value, canonical_unit, source_id, admission "
        "FROM fact WHERE soft_deleted_at IS NULL")).fetchall()

    buckets: dict[tuple, list] = {}
    for r in rows:
        try:
            pd = profile.property(r["property_name"])
        except KeyError:
            stats["orphaned"] += 1
            if on_removed == "soft_delete":
                await conn.execute("UPDATE fact SET soft_deleted_at=? WHERE id=?", (now, r["id"]))
            continue
        quals = json.loads(r["qualifiers_json"] or "{}")
        ident = {q: quals.get(q) for q in pd.identity_qualifiers}
        new_tk = _identity.tuple_key(r["instance_key"], pd.name, ident)
        if new_tk != r["tuple_key"]:
            await conn.execute("UPDATE fact SET tuple_key=? WHERE id=?", (new_tk, r["id"]))
            stats["tuple_keys_changed"] += 1
        src = await (await conn.execute(
            "SELECT url_or_domain FROM source WHERE id=?", (r["source_id"],))).fetchone()
        url = src["url_or_domain"] if src else ""
        meets = registry.meets_bar(url, getattr(pd, "trust_threshold", "reputable"))
        has_unspec = any(ident.get(q) is None for q in (pd.required_qualifiers or []))
        f = _model.Fact(
            fact_id=r["id"], tuple_key=new_tk, as_of=r["as_of"], value=r["value"], unit=r["unit"],
            source_meets_bar=meets, has_unspecified_required=has_unspec, admission=r["admission"],
            canonical_value=r["canonical_value"], canonical_unit=r["canonical_unit"])
        buckets.setdefault((new_tk, r["as_of"]), []).append(f)

    # 4) Clear OPEN conflicts (re-derived below); preserve resolved (adjudicated) history.
    await conn.execute(
        "DELETE FROM conflict_member WHERE conflict_id IN (SELECT id FROM conflict WHERE status='open')")
    await conn.execute("DELETE FROM conflict WHERE status='open'")

    # 5) Re-run conflict detection + promotion/demotion per bucket (reuse the ingest logic).
    for (tk, as_of), bucket in buckets.items():
        intents = _conflict.detect(bucket)
        has_open = False
        for intent in intents:
            if isinstance(intent, _model.OpenConflict):
                has_open = True
                cc = await conn.execute(
                    "INSERT INTO conflict (tuple_key, as_of, status, created_at) VALUES (?,?, 'open', ?)",
                    (tk, as_of, now))
                stats["conflicts_opened"] += 1
                for fid in intent.fact_ids:
                    await conn.execute(
                        "INSERT INTO conflict_member (conflict_id, fact_id) VALUES (?,?)",
                        (cc.lastrowid, fid))
        for f in bucket:
            ev = _promotion.evaluate(f, bucket, has_open_conflict=has_open)
            if isinstance(ev, _model.Promote):
                await conn.execute("UPDATE fact SET admission='trusted' WHERE id=?", (f.fact_id,))
                stats["promoted"] += 1
            elif isinstance(ev, _model.Demote):
                await conn.execute("UPDATE fact SET admission='provisional' WHERE id=?", (f.fact_id,))
                stats["demoted"] += 1

    await conn.commit()
    return stats
```

- [ ] **Step 4: Run, verify pass**

Run: `uv run pytest tests/test_factbase_rebuild.py -q`
Expected: PASS (3 passed). If `test_dropping_identity_qualifier...` fails on the conflict/demotion count, inspect: the two seeded facts must both be trust-bar (authoritative sources in REG), share the new tuple_key, and have distinct canonical values — verify `conflict.detect` sees them in one `as_of` group. Do NOT weaken the test; fix the reconstruction (likely `source_meets_bar` or `canonical_value`).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/rebuild.py tests/test_factbase_rebuild.py
git commit -m "feat(factbase): structural rebuild (re-derive tuple_key/conflict/promotion)"
```

---

## Task 2: `dossier recompute --rebuild`

**Files:**
- Modify: `src/open_deep_research/factbase/dossier.py`
- Test: `tests/test_dossier_recompute.py` (append)

- [ ] **Step 1: Append the failing test**

Append to `tests/test_dossier_recompute.py`:

```python
def test_recompute_rebuild_runs(tmp_path):
    db = str(tmp_path / "fb.db")

    async def go():
        out = await dossier.run(["recompute", "--rebuild"], db_path=db)
        assert "rebuild" in out.lower()

    asyncio.run(go())
```

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_dossier_recompute.py::test_recompute_rebuild_runs -q`
Expected: FAIL (`--rebuild` not a recognized arg).

- [ ] **Step 3: Add the flag + handler branch**

In `src/open_deep_research/factbase/dossier.py`, in `_parser()`, add to the `rec` subparser (after the `--check` argument):

```python
    rec.add_argument("--rebuild", action="store_true",
                     help="Structural rebuild: re-derive tuple_key, conflicts, promotion (after identity/enum/threshold edits).")
    rec.add_argument("--rename", action="append", default=[], metavar="OLD=NEW",
                     help="Rename a property during rebuild (repeatable).")
    rec.add_argument("--on-removed", choices=["retain", "soft_delete"], default="retain",
                     help="Policy for facts whose property was removed from the profile.")
```

In `run()`, inside the existing `if args.command == "recompute":` block, BEFORE the existing `if args.check:` line, add a rebuild branch:

```python
        if getattr(args, "rebuild", False):
            from open_deep_research.factbase import (
                rebuild as _rebuild, registry as _registry, migrations as _mig2, schema as _schema2)
            from open_deep_research import storage as _storage2
            reg = _registry.SourceRegistry.load("di_source_registry")
            rename = dict(pair.split("=", 1) for pair in args.rename) if args.rename else {}
            async with aiosqlite.connect(db_path) as conn:
                await _storage2._ensure_schema(conn)
                await _mig2.apply(conn, _schema2.STEPS)
                stats = await _rebuild.rebuild_structural(
                    conn, prof, reg, rename=rename, on_removed=args.on_removed)
            return ("rebuild complete for " + args.profile + ": "
                    + ", ".join(f"{k}={v}" for k, v in stats.items()))
```

(`prof` is already loaded just above in the recompute block: `prof = _profile.load(args.profile)`.)

- [ ] **Step 4: Run, verify pass**

Run: `uv run pytest tests/test_dossier_recompute.py -q`
Expected: PASS (3 passed: the two existing + the new rebuild test).

- [ ] **Step 5: Full suite + CLI smoke**

Run: `uv run pytest -q && uv run dossier recompute --rebuild; echo "exit=$?"`
Expected: all PASS; the CLI prints a `rebuild complete ...` line and exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/factbase/dossier.py tests/test_dossier_recompute.py
git commit -m "feat(dossier): recompute --rebuild (structural) with --rename/--on-removed"
```

---

## Final verification

- [ ] **Full suite:** `uv run pytest -q` → all green.
- [ ] **CI gate:** `uv run dossier validate; echo "exit=$?"` → exit 0.
- [ ] **Rebuild semantics:** the Task-1 tests prove the headline property — dropping an identity qualifier collapses tuples and opens a conflict + demotes; rename moves facts; orphan policy honored.
- [ ] **Reuse, not reimplement:** confirm `rebuild.py` calls `conflict.detect` and `promotion.evaluate` (the same functions ingest uses) — `grep -n "conflict.detect\|promotion.evaluate" src/open_deep_research/factbase/rebuild.py`.

---

## Self-review notes (author)

- **Spec coverage (6b-2 slice):** structural `--rebuild` re-derives tuple_key + conflict + promotion ✓ (T1); rename-map ✓ (T1,T2); orphan policy (retain/soft_delete) ✓ (T1,T2); demotion (which ingest lacks) ✓ (T1). Reuses `conflict.detect`/`promotion.evaluate` (no reimplementation). Preserves resolved conflicts.
- **Placeholder scan:** none.
- **Type consistency:** `rebuild_structural(conn, profile, registry, *, rename, on_removed) -> dict(stats)` defined T1, called by the CLI T2; reconstructs `model.Fact(...)` with the exact field names from `model.py`; recomputes `tuple_key` via `identity.tuple_key` with `pd.identity_qualifiers` (matching `ingest.py:42`); `registry.meets_bar(url, pd.trust_threshold)`.
- **Risk note:** the Task-4 fix guidance forbids weakening tests; the seeded-fact tests assert observable conflict/admission outcomes, which is the correctness contract.
