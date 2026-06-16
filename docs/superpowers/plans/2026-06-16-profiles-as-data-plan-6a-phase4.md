# Profiles as Data — Plan 6a Phase 4 (Drift Detection + Recompute) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect when the active profile has changed since the last stamped run (the un-versioned-edit erosion signal) and give the user a way to refresh: a `factbase/drift.py` check, an in-graph warning, and a `dossier recompute [--check]` CLI (`--check` = dry-run drift report; bare = force-recompute canonical values).

**Architecture:** Runs are already stamped with `profile_hash` (Phase 2). A DB-aware `check_drift` compares the *loaded* profile's hash to the latest stamped run for that profile (NOT in pure `load()`). The graph warns on mismatch (warn-and-proceed). `dossier recompute` drives the existing `recompute.backfill_canonical_values(force=True)` (normalization recompute); `--check` only reports.

**Tech Stack:** Python 3.11, `aiosqlite`, the factbase migration framework, pytest. No new deps.

**Builds on:** Phases 1-3 (merged). Phase 2 added the `profile_name/profile_version/profile_hash` columns + per-run stamping; Phase 2 also gives `Profile.profile_hash`.

**Scope:** drift detection + warning + `dossier recompute`/`--check` (normalization recompute, `force=True`). **Deferred to Plan 6b:** the *structural* `--rebuild` (tuple-key/conflict/promotion rebuild) and scaffolding.

---

## File structure

| File | Responsibility | Action |
|---|---|---|
| `src/open_deep_research/factbase/drift.py` | `latest_run_profile_hash` + `check_drift` (DB-aware) | Create |
| `src/open_deep_research/factbase/dossier.py` | `recompute [--check] [--profile]` subcommand | Modify |
| `src/open_deep_research/deep_researcher.py` | warn on profile drift in `extract_facts` | Modify (stamp block) |
| `tests/test_factbase_drift.py` | `check_drift` behavior | Create |
| `tests/test_dossier_recompute.py` | CLI `--check` (drift / no-drift) + action | Create |

---

## Task 1: `factbase/drift.py`

**Files:**
- Create: `src/open_deep_research/factbase/drift.py`
- Test: `tests/test_factbase_drift.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_factbase_drift.py`:

```python
import asyncio

from open_deep_research import storage
from open_deep_research.factbase.drift import check_drift


def test_check_drift_same_hash_no_drift(tmp_path):
    db = str(tmp_path / "fb.db")

    async def go():
        rid = await storage.preallocate_run(db, "t")
        await storage.finalize_research_run(db, rid, {
            "profile_name": "p", "profile_hash": "hash_A", "status": "completed"})
        d = await check_drift(db, "p", "hash_A")
        assert d["drifted"] is False and d["last_run_hash"] == "hash_A"

    asyncio.run(go())


def test_check_drift_different_hash_drifts(tmp_path):
    db = str(tmp_path / "fb.db")

    async def go():
        rid = await storage.preallocate_run(db, "t")
        await storage.finalize_research_run(db, rid, {
            "profile_name": "p", "profile_hash": "hash_A", "status": "completed"})
        d = await check_drift(db, "p", "hash_B")
        assert d["drifted"] is True and d["last_run_hash"] == "hash_A"

    asyncio.run(go())


def test_check_drift_no_prior_run(tmp_path):
    db = str(tmp_path / "fb.db")

    async def go():
        await storage.preallocate_run(db, "t")  # ensures schema exists, no stamped run
        d = await check_drift(db, "unseen", "whatever")
        assert d["drifted"] is False and d["last_run_hash"] is None

    asyncio.run(go())
```

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_factbase_drift.py -q`
Expected: FAIL (`ModuleNotFoundError: ...drift`).

- [ ] **Step 3: Implement**

Create `src/open_deep_research/factbase/drift.py`:

```python
"""Profile-drift detection: has the active profile changed since the last stamped run?

Phase 2 stamps every run with profile_name/profile_version/profile_hash. This compares the
*currently loaded* profile's hash to the latest stamped run for that profile name. A mismatch
(same version, different hash) is the un-versioned-edit erosion signal. Read-only — never
auto-recomputes. Intentionally a DB-aware caller, NOT part of the pure profile.load().
"""
from __future__ import annotations

import aiosqlite

from . import migrations, schema


async def latest_run_profile_hash(db_path: str, profile_name: str) -> str | None:
    """Return the most recent stamped profile_hash for ``profile_name``, or None."""
    from open_deep_research import storage
    async with aiosqlite.connect(db_path) as conn:
        await storage._ensure_schema(conn)          # research_runs base table
        await migrations.apply(conn, schema.STEPS)   # v6 profile columns
        cur = await conn.execute(
            "SELECT profile_hash FROM research_runs "
            "WHERE profile_name=? AND profile_hash IS NOT NULL ORDER BY id DESC LIMIT 1",
            (profile_name,),
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def check_drift(db_path: str, profile_name: str, current_hash: str | None) -> dict:
    """Compare ``current_hash`` to the latest stamped run for ``profile_name``."""
    last = await latest_run_profile_hash(db_path, profile_name)
    drifted = bool(last) and bool(current_hash) and last != current_hash
    return {
        "profile_name": profile_name,
        "current_hash": current_hash,
        "last_run_hash": last,
        "drifted": drifted,
    }
```

- [ ] **Step 4: Run, verify pass**

Run: `uv run pytest tests/test_factbase_drift.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/drift.py tests/test_factbase_drift.py
git commit -m "feat(factbase): profile-drift detection (check_drift)"
```

---

## Task 2: `dossier recompute [--check]`

**Files:**
- Modify: `src/open_deep_research/factbase/dossier.py`
- Test: `tests/test_dossier_recompute.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_dossier_recompute.py`:

```python
import asyncio

from open_deep_research import storage
from open_deep_research.factbase import dossier, profile


def test_recompute_check_reports_drift_then_clear(tmp_path):
    db = str(tmp_path / "fb.db")
    real_hash = profile.load("country_digital_identity").profile_hash

    async def go():
        rid = await storage.preallocate_run(db, "t")
        await storage.finalize_research_run(db, rid, {
            "profile_name": "country_digital_identity",
            "profile_hash": "STALE", "status": "completed"})
        out = await dossier.run(["recompute", "--check"], db_path=db)
        assert "DRIFT" in out

        rid2 = await storage.preallocate_run(db, "t2")
        await storage.finalize_research_run(db, rid2, {
            "profile_name": "country_digital_identity",
            "profile_hash": real_hash, "status": "completed"})
        out2 = await dossier.run(["recompute", "--check"], db_path=db)
        assert "no drift" in out2

    asyncio.run(go())


def test_recompute_action_runs_on_empty_factbase(tmp_path):
    db = str(tmp_path / "fb.db")

    async def go():
        out = await dossier.run(["recompute"], db_path=db)
        assert "recomputed" in out and "0" in out  # empty fact table -> 0 rows

    asyncio.run(go())
```

- [ ] **Step 2: Run, verify it fails**

Run: `uv run pytest tests/test_dossier_recompute.py -q`
Expected: FAIL (argparse: invalid choice 'recompute').

- [ ] **Step 3: Register the subcommand**

In `src/open_deep_research/factbase/dossier.py`, in `_parser()`, after the line `sub.add_parser("validate", ...)` and before `return parser`, add:

```python
    rec = sub.add_parser("recompute", help="Recompute canonical fact values; --check reports drift only.")
    rec.add_argument("--profile", default="country_digital_identity",
                     help="Profile name (YAML stem) to check/recompute against.")
    rec.add_argument("--check", action="store_true",
                     help="Report whether the profile changed since the last stamped run (no writes).")
```

- [ ] **Step 4: Handle it in `run()`**

In `run()`, AFTER the line `db_path = db_path or get_db_path(None)` and BEFORE the `async with aiosqlite.connect(db_path) as conn:` block, insert:

```python
    if args.command == "recompute":
        from open_deep_research import storage as _storage
        from open_deep_research.factbase import (
            drift as _drift, migrations as _mig, profile as _profile,
            recompute as _recompute, schema as _schema,
        )
        prof = _profile.load(args.profile)
        cur_hash = getattr(prof, "profile_hash", None)
        if args.check:
            d = await _drift.check_drift(db_path, args.profile, cur_hash)
            if d["drifted"]:
                return (f"DRIFT  {args.profile}: changed since last run "
                        f"({(d['last_run_hash'] or '')[:8]} -> {(cur_hash or '')[:8]}). "
                        f"Run `dossier recompute --profile {args.profile}` to refresh canonical values.")
            return f"OK     {args.profile}: no drift (hash {(cur_hash or 'none')[:8]})."
        async with aiosqlite.connect(db_path) as conn:
            await _storage._ensure_schema(conn)
            await _mig.apply(conn, _schema.STEPS)
            n = await _recompute.backfill_canonical_values(conn, prof, force=True)
        return f"recomputed canonical values for {n} fact row(s) under {args.profile}."
```

- [ ] **Step 5: Run, verify pass**

Run: `uv run pytest tests/test_dossier_recompute.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: CLI smoke**

Run: `uv run dossier recompute --check; echo "exit=$?"`
Expected: prints a line (likely `OK ... no drift` or `DRIFT ...` depending on local DB) and `exit=0`.

- [ ] **Step 7: Commit**

```bash
git add src/open_deep_research/factbase/dossier.py tests/test_dossier_recompute.py
git commit -m "feat(dossier): recompute subcommand (--check drift report; force canonical backfill)"
```

---

## Task 3: Warn on drift in the graph

**Files:**
- Modify: `src/open_deep_research/deep_researcher.py` (the stamp block in `extract_facts`)

- [ ] **Step 1: Add the warning before the stamp UPDATE**

In `extract_facts`, find the provenance stamp block added in Phase 2 (inside `if run_id:`, the `UPDATE research_runs SET profile_name=?, profile_version=?, profile_hash=? ...`). Immediately BEFORE that `await conn.execute("UPDATE research_runs SET profile_name=?, ...")` line (but still inside `if run_id:`), insert:

```python
                # Drift signal: if a *prior* run used a different hash for this profile, warn
                # (warn-and-proceed). The current run isn't stamped yet, so exclude its id.
                _cur = await conn.execute(
                    "SELECT profile_hash FROM research_runs "
                    "WHERE profile_name=? AND profile_hash IS NOT NULL AND id<>? "
                    "ORDER BY id DESC LIMIT 1",
                    (configurable.profile_name, run_id))
                _prev = await _cur.fetchone()
                _cur_hash = getattr(prof, "profile_hash", None)
                if _prev and _prev[0] and _cur_hash and _prev[0] != _cur_hash:
                    logger.warning(
                        "Profile '%s' changed since the last run (%s -> %s); prior facts may be "
                        "stale until `dossier recompute --profile %s`.",
                        configurable.profile_name, _prev[0][:8], _cur_hash[:8], configurable.profile_name)
```

Keep the existing `UPDATE ... commit()` immediately after. (The query uses the already-open `conn`, so no nested connection.)

- [ ] **Step 2: Import check**

Run: `uv run python -c "import open_deep_research.deep_researcher"`
Expected: no error.

- [ ] **Step 3: Full suite**

Run: `uv run pytest -q`
Expected: all PASS (the warning is additive; no test stamps two differing hashes for one profile, so it stays silent in tests).

- [ ] **Step 4: Commit**

```bash
git add src/open_deep_research/deep_researcher.py
git commit -m "feat(factbase): warn when the selected profile drifted from the last run"
```

---

## Final verification

- [ ] **Full suite:** `uv run pytest -q` → all green.
- [ ] **CI gate:** `uv run dossier validate; echo "exit=$?"` → exit 0.
- [ ] **Drift CLI works:** `uv run dossier recompute --check` prints a drift/no-drift line and exits 0.
- [ ] **Recompute action:** `uv run dossier recompute` reports a row count and exits 0.

---

## Self-review notes (author)

- **Spec coverage (Phase-4 slice):** DB-aware drift detection (NOT in pure `load()`) ✓ (T1); warn-and-proceed in the graph ✓ (T3); `dossier recompute --check` dry-run + `dossier recompute` normalization recompute (force backfill) ✓ (T2). Deferred to 6b: structural `--rebuild` (tuple-key/conflict/promotion) + rename-map/orphan policy; scaffolding.
- **Placeholder scan:** none.
- **Type consistency:** `check_drift(db_path, profile_name, current_hash) -> dict` (keys `drifted`/`last_run_hash`/`current_hash`/`profile_name`) defined in T1, consumed by the CLI in T2; the graph warning (T3) inlines the same SELECT against the open `conn` rather than calling `check_drift` (avoids a nested connection); `recompute.backfill_canonical_values(conn, prof, force=True)` reused unchanged; column names match the Phase-2 migration v6.
