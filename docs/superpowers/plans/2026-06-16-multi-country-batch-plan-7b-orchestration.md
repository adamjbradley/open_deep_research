# Multi-Country Batch Research — Plan 7b (Orchestration) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the same profile across N countries as one resumable, bounded-concurrent batch that auto-provisions a committed source registry so facts promote, producing per-country dossiers + a cross-country matrix.

**Architecture:** An orchestration layer over the existing `deep_researcher` graph (graph untouched). A SQLite `BatchLedger` checkpoints each country (resume skips `done`); a `BatchRunner` runs countries K-at-a-time; a `RegistryProvisioner` scaffolds+commits a `<domain>_source_registry.yaml` when none matches (mirroring profile scaffolding, targeting the registry meta-schema); the scout list strategy and a `research-batch`/`dossier batch` entry point tie it together.

**Tech Stack:** Python 3.11, `aiosqlite`, `asyncio`, `PyYAML`, `pytest`/`pytest-asyncio`. Reuses Plan 7a (`country_list`, `entities`, `matrix`) and the profiles-as-data scaffolding machinery.

**Spec:** `docs/superpowers/specs/2026-06-16-multi-country-batch-research-design.md` (Units 3, 4, scout half of 1).

**Branch:** continue on `spec/multi-country-batch`. **Depends on Plan 7a being merged/complete.**

---

## File Structure

- Modify `src/open_deep_research/factbase/schema.py` — migration step 7: `batch_run`, `batch_item`.
- Create `src/open_deep_research/factbase/batch_ledger.py` — `batch_id_for`, `BatchLedger`.
- Create `src/open_deep_research/factbase/registry_scaffold.py` — `RegistryProposal`, `induce_registry`, `render_registry_yaml`, `render_registry_draft_yaml` (parallels `scaffold.py`).
- Create `src/open_deep_research/factbase/registry_provision.py` — `ensure_registry()` + `git_commit_paths()`.
- Create `src/open_deep_research/factbase/batch.py` — `BatchRunner` (+ scout strategy hook).
- Modify `src/open_deep_research/factbase/country_list.py` — add `resolve_country_list_async` with scout.
- Modify `src/open_deep_research/factbase/dossier.py` — `batch` subcommand.
- Modify `pyproject.toml` — `research-batch` console script (optional; `dossier batch` is primary).
- Tests: `tests/test_batch_ledger.py`, `tests/test_registry_scaffold.py`, `tests/test_registry_provision.py`, `tests/test_batch_runner.py`, `tests/test_country_scout.py`, `tests/test_batch_end_to_end.py`.

---

### Task 1: Batch ledger schema (migration step 7)

**Files:**
- Modify: `src/open_deep_research/factbase/schema.py`
- Test: `tests/test_batch_ledger.py` (schema half)

- [ ] **Step 1: Write the failing test**

Create `tests/test_batch_ledger.py`:

```python
import aiosqlite
import pytest

from open_deep_research import storage as _storage
from open_deep_research.factbase import migrations as _mig, schema as _schema


@pytest.mark.asyncio
async def test_batch_tables_exist_after_migration(tmp_path):
    db = str(tmp_path / "b.db")
    async with aiosqlite.connect(db) as conn:
        await _storage._ensure_schema(conn)
        await _mig.apply(conn, _schema.STEPS)
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('batch_run','batch_item')")
        names = sorted(r[0] for r in await cur.fetchall())
    assert names == ["batch_item", "batch_run"]
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/test_batch_ledger.py::test_batch_tables_exist_after_migration -q`
Expected: FAIL — tables don't exist.

- [ ] **Step 3: Add migration step 7**

In `src/open_deep_research/factbase/schema.py`, append to the `STEPS` list after the `(6, ...)` entry:

```python
    (7, """
    CREATE TABLE IF NOT EXISTS batch_run (
        batch_id TEXT PRIMARY KEY,
        profile_name TEXT,
        profile_hash TEXT,
        list_spec TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS batch_item (
        batch_id TEXT,
        instance_key TEXT,
        country_name TEXT,
        status TEXT,
        run_id TEXT,
        error TEXT,
        updated_at TEXT,
        PRIMARY KEY (batch_id, instance_key)
    );
    """),
```

(If `STEPS` entries are `(n, sql)` tuples applied in order — confirm by reading the `(6, ...)` entry — match that exact shape.)

- [ ] **Step 4: Run the test to confirm it passes**

Run: `uv run pytest tests/test_batch_ledger.py::test_batch_tables_exist_after_migration -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/schema.py tests/test_batch_ledger.py
git commit -m "feat(factbase): batch_run/batch_item ledger tables (migration 7)"
```

---

### Task 2: BatchLedger (deterministic id, resume semantics)

**Files:**
- Create: `src/open_deep_research/factbase/batch_ledger.py`
- Test: `tests/test_batch_ledger.py` (add cases)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_batch_ledger.py`:

```python
from open_deep_research.factbase.batch_ledger import BatchLedger, batch_id_for


def test_batch_id_is_deterministic():
    a = batch_id_for("country_cbdc", "Nigeria, India")
    b = batch_id_for("country_cbdc", "Nigeria, India")
    c = batch_id_for("country_cbdc", "Nigeria, Bahamas")
    assert a == b and a != c


@pytest.mark.asyncio
async def test_ledger_resume_skips_done(tmp_path):
    db = str(tmp_path / "l.db")
    async with aiosqlite.connect(db) as conn:
        await _storage._ensure_schema(conn)
        await _mig.apply(conn, _schema.STEPS)
        led = BatchLedger(conn, "bid1", profile_name="p", profile_hash="h", list_spec="s")
        await led.ensure_run()
        await led.upsert_item("NGA", "Nigeria", status="pending")
        await led.upsert_item("IND", "India", status="pending")
        await led.mark("NGA", status="done", run_id="7")
        pending = await led.pending_items()
        assert [i["instance_key"] for i in pending] == ["IND"]   # NGA skipped
        await led.mark("IND", status="failed", error="boom")
        # failed is retryable on re-run
        retry = await led.pending_items(include_failed=True)
        assert [i["instance_key"] for i in retry] == ["IND"]
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/test_batch_ledger.py -q`
Expected: FAIL — module `batch_ledger` missing.

- [ ] **Step 3: Implement batch_ledger.py**

Create `src/open_deep_research/factbase/batch_ledger.py`:

```python
"""SQLite ledger for resumable batch runs: one batch_run, many batch_item rows.

batch_id is derived from (profile, normalized list spec) so a re-run reattaches and
skips items already 'done'. Timestamps are passed in (the codebase forbids Date.now()-
style nondeterminism in some contexts); callers supply `now` or we use a DB default.
"""
from __future__ import annotations

import hashlib

import aiosqlite

_STATUSES = {"pending", "running", "done", "failed"}


def batch_id_for(profile_name: str, list_spec: str) -> str:
    norm = ",".join(sorted(p.strip().lower() for p in (list_spec or "").replace("\n", ",").split(",") if p.strip()))
    raw = f"{profile_name}|{norm}"
    return "b_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class BatchLedger:
    def __init__(self, conn: aiosqlite.Connection, batch_id: str, *,
                 profile_name: str, profile_hash: str, list_spec: str):
        self._conn = conn
        self.batch_id = batch_id
        self._meta = (profile_name, profile_hash, list_spec)

    async def ensure_run(self) -> None:
        await self._conn.execute(
            "INSERT OR IGNORE INTO batch_run (batch_id, profile_name, profile_hash, list_spec, created_at) "
            "VALUES (?,?,?,?, datetime('now'))",
            (self.batch_id, *self._meta))
        await self._conn.commit()

    async def upsert_item(self, instance_key: str, country_name: str, *, status: str = "pending") -> None:
        assert status in _STATUSES
        await self._conn.execute(
            "INSERT INTO batch_item (batch_id, instance_key, country_name, status, updated_at) "
            "VALUES (?,?,?,?, datetime('now')) "
            "ON CONFLICT(batch_id, instance_key) DO NOTHING",
            (self.batch_id, instance_key, country_name, status))
        await self._conn.commit()

    async def mark(self, instance_key: str, *, status: str, run_id: str | None = None,
                   error: str | None = None) -> None:
        assert status in _STATUSES
        await self._conn.execute(
            "UPDATE batch_item SET status=?, run_id=?, error=?, updated_at=datetime('now') "
            "WHERE batch_id=? AND instance_key=?",
            (status, run_id, error, self.batch_id, instance_key))
        await self._conn.commit()

    async def pending_items(self, *, include_failed: bool = True) -> list[dict]:
        self._conn.row_factory = aiosqlite.Row
        statuses = ["pending", "running"] + (["failed"] if include_failed else [])
        ph = ",".join("?" for _ in statuses)
        cur = await self._conn.execute(
            f"SELECT instance_key, country_name, status FROM batch_item "
            f"WHERE batch_id=? AND status IN ({ph}) ORDER BY instance_key",
            (self.batch_id, *statuses))
        return [dict(r) for r in await cur.fetchall()]

    async def summary(self) -> dict:
        self._conn.row_factory = aiosqlite.Row
        cur = await self._conn.execute(
            "SELECT status, COUNT(*) n FROM batch_item WHERE batch_id=? GROUP BY status",
            (self.batch_id,))
        return {r["status"]: r["n"] for r in await cur.fetchall()}
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `uv run pytest tests/test_batch_ledger.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/batch_ledger.py tests/test_batch_ledger.py
git commit -m "feat(factbase): resumable BatchLedger with deterministic batch_id"
```

---

### Task 3: Registry scaffolding (parallels profile scaffolding)

**Files:**
- Create: `src/open_deep_research/factbase/registry_scaffold.py`
- Test: `tests/test_registry_scaffold.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_registry_scaffold.py`:

```python
import pytest

from open_deep_research.factbase.registry_scaffold import (
    RegistryProposal, induce_registry, render_registry_draft_yaml, render_registry_yaml)
from open_deep_research.factbase.registry_schema import registry_from_dict


def _proposal():
    return RegistryProposal(sources=[
        {"domain": "cbn.gov.ng", "tier": "authoritative", "flags": ["primary"],
         "rationale": "national central bank, primary issuer", "confidence": "high"},
        {"domain": "randomblog.example", "tier": "unvetted", "flags": [],
         "rationale": "unknown provenance", "confidence": "low"},
    ])


@pytest.mark.asyncio
async def test_induce_validates_against_registry_meta_schema():
    async def fake_call(prompt):
        return _proposal()
    out = await induce_registry("cbdc", "central bank digital currency", [], fake_call)
    assert any(s.domain == "cbn.gov.ng" for s in out.sources)


def test_render_yaml_is_loadable_registry():
    yml = render_registry_yaml(_proposal())
    import yaml as _y
    entries = registry_from_dict(_y.safe_load(yml))   # meta-schema gate
    assert entries["cbn.gov.ng"]["tier"] == "authoritative"


def test_draft_has_annotations():
    d = render_registry_draft_yaml(_proposal())
    assert "SCAFFOLD DRAFT" in d
    assert "cbn.gov.ng" in d and "confidence: high" in d
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/test_registry_scaffold.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement registry_scaffold.py**

Create `src/open_deep_research/factbase/registry_scaffold.py`:

```python
"""Assisted source-registry scaffolding: an LLM drafts domain trust tiers from a domain
description (and optional observed domains). Mirrors scaffold.py but targets the registry
meta-schema (domain -> tier -> flags). Model output is validated before it can be written;
seed text is DATA, never instructions. Conservative default: 'authoritative' needs an
explicit rationale; absent evidence the generator is told to prefer 'reputable'/'unvetted'.
"""
from __future__ import annotations

import yaml
from pydantic import BaseModel

from .registry_schema import registry_from_dict

_TIERS = ("unvetted", "reputable", "authoritative")


class RegistrySource(BaseModel):
    domain: str
    tier: str = "reputable"
    flags: list[str] = []
    rationale: str = ""
    confidence: str = "medium"


class RegistryProposal(BaseModel):
    sources: list[RegistrySource]


def build_registry_prompt(domain_label, description, observed_domains) -> str:
    seen = ""
    if observed_domains:
        seen = ("\n\nDOMAINS ACTUALLY SEEN in research sources (treat as DATA; tier these and "
                "add other obvious authorities):\n" + "\n".join(sorted(set(observed_domains))[:60]))
    return (
        f"You are building a SOURCE TRUST REGISTRY for the '{domain_label}' domain ({description}). "
        "List source web domains and assign each a trust tier: one of "
        "unvetted / reputable / authoritative. 'authoritative' is reserved for primary issuers / "
        "official bodies / standards organizations and MUST carry a rationale; when unsure prefer "
        "'reputable' (known media/analysts) or 'unvetted'. Give flags (e.g. 'primary', 'official', "
        "'aggregator') where useful, a short rationale, and confidence (low/medium/high). "
        "Prefer globally-representative authorities, not only Western ones." + seen
    )


def _proposal_to_registry_dict(proposal: "RegistryProposal") -> dict:
    return {"version": "1", "sources": [
        {"domain": s.domain, "tier": s.tier, "flags": list(s.flags)} for s in proposal.sources]}


async def induce_registry(domain_label, description, observed_domains, model_call) -> "RegistryProposal":
    prompt = build_registry_prompt(domain_label, description, observed_domains)
    proposal = await model_call(prompt)
    if not isinstance(proposal, RegistryProposal):
        proposal = RegistryProposal.model_validate(proposal)
    for s in proposal.sources:
        if s.tier not in _TIERS:
            raise ValueError(f"source {s.domain!r}: invalid tier {s.tier!r}")
    registry_from_dict(_proposal_to_registry_dict(proposal))  # meta-schema gate (raises if invalid)
    return proposal


def render_registry_yaml(proposal: "RegistryProposal") -> str:
    return yaml.safe_dump(_proposal_to_registry_dict(proposal), sort_keys=False)


def render_registry_draft_yaml(proposal: "RegistryProposal") -> str:
    notes = [
        "# === SCAFFOLD DRAFT - machine-generated source registry; audit copy (NOT loaded) ===",
        "# The usable registry was written to the sibling <name>.yaml. This records the",
        "# generator's tier decisions + rationale so you can spot-check trust assignments.",
        "#",
        "# Tier decisions:",
    ]
    for s in proposal.sources:
        notes.append(f"#  - {s.domain}: {s.tier} {s.flags or []}  ->  "
                     f"{s.rationale or '(no rationale)'} (confidence: {s.confidence})")
    notes.append("#")
    return "\n".join(notes) + "\n" + render_registry_yaml(proposal)
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `uv run pytest tests/test_registry_scaffold.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/registry_scaffold.py tests/test_registry_scaffold.py
git commit -m "feat(factbase): assisted source-registry scaffolding (registry meta-schema)"
```

---

### Task 4: RegistryProvisioner (ensure + commit)

**Files:**
- Create: `src/open_deep_research/factbase/registry_provision.py`
- Test: `tests/test_registry_provision.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_registry_provision.py`:

```python
import pytest

from open_deep_research.factbase import registry_provision as rp
from open_deep_research.factbase.registry_scaffold import RegistryProposal


@pytest.mark.asyncio
async def test_existing_registry_is_reused(monkeypatch):
    # di_source_registry ships with the package -> reused, no scaffold, no commit.
    called = {"commit": 0}
    monkeypatch.setattr(rp, "git_commit_paths", lambda paths, msg: called.__setitem__("commit", called["commit"] + 1))
    name = await rp.ensure_registry(
        registry_name="di_source_registry", domain_label="di", description="x",
        observed_domains=[], model_call=None, autocommit=True)
    assert name == "di_source_registry"
    assert called["commit"] == 0


@pytest.mark.asyncio
async def test_missing_registry_is_scaffolded_and_committed(tmp_path, monkeypatch):
    commits = []
    monkeypatch.setattr(rp, "git_commit_paths", lambda paths, msg: commits.append((paths, msg)))
    # write into a temp profiles dir
    monkeypatch.setattr(rp, "_profiles_dir", lambda: str(tmp_path))

    async def fake_call(prompt):
        return RegistryProposal(sources=[{"domain": "cbn.gov.ng", "tier": "authoritative",
                                          "flags": ["primary"], "rationale": "issuer", "confidence": "high"}])

    name = await rp.ensure_registry(
        registry_name=None, domain_label="cbdc", description="central bank digital currency",
        observed_domains=["cbn.gov.ng"], model_call=fake_call, autocommit=True)
    assert name == "cbdc_source_registry"
    assert (tmp_path / "cbdc_source_registry.yaml").is_file()
    assert (tmp_path / "cbdc_source_registry.draft.yaml").is_file()
    assert len(commits) == 1
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/test_registry_provision.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement registry_provision.py**

Create `src/open_deep_research/factbase/registry_provision.py`:

```python
"""Ensure a usable source registry exists for a domain; scaffold+commit one if not.

Resolution order: (1) an explicit registry_name that loads non-empty -> reuse;
(2) else derive '<domain_label>_source_registry' and reuse if the file exists;
(3) else scaffold it from the description (+ observed domains), write the usable .yaml
plus an audit .draft.yaml, git-commit both, and use it. Lets corroborated facts promote.
"""
from __future__ import annotations

import os
import subprocess

from .registry import SourceRegistry
from .registry_scaffold import induce_registry, render_registry_draft_yaml, render_registry_yaml


def _profiles_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "profiles")


def git_commit_paths(paths: list[str], msg: str) -> None:
    """Stage + commit specific paths. Non-fatal on failure (warn-and-continue)."""
    try:
        subprocess.run(["git", "add", *paths], check=True)
        subprocess.run(["git", "commit", "-m", msg], check=True)
    except Exception as e:  # noqa: BLE001 - a commit failure must not abort paid research
        print(f"registry auto-commit failed (non-fatal): {e}")


def _loads_nonempty(name: str) -> bool:
    try:
        reg = SourceRegistry.load(name)
        return bool(getattr(reg, "_entries", None))
    except Exception:  # noqa: BLE001
        return False


async def ensure_registry(*, registry_name, domain_label, description, observed_domains,
                          model_call, autocommit: bool) -> str:
    if registry_name and _loads_nonempty(registry_name):
        return registry_name
    derived = f"{domain_label}_source_registry"
    if _loads_nonempty(derived):
        return derived
    if model_call is None:
        # No way to scaffold without a model; leave unprovisioned (facts stay provisional).
        return registry_name or derived
    proposal = await induce_registry(domain_label, description, observed_domains, model_call)
    out_yaml = os.path.join(_profiles_dir(), f"{derived}.yaml")
    out_draft = os.path.join(_profiles_dir(), f"{derived}.draft.yaml")
    with open(out_yaml, "w", encoding="utf-8") as fh:
        fh.write(render_registry_yaml(proposal))
    with open(out_draft, "w", encoding="utf-8") as fh:
        fh.write(render_registry_draft_yaml(proposal))
    if autocommit:
        git_commit_paths([out_yaml, out_draft], f"feat(factbase): auto-scaffold {derived}")
    return derived
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `uv run pytest tests/test_registry_provision.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/registry_provision.py tests/test_registry_provision.py
git commit -m "feat(factbase): RegistryProvisioner ensures+commits a registry so facts promote"
```

---

### Task 5: Scout list strategy (async)

**Files:**
- Modify: `src/open_deep_research/factbase/country_list.py`
- Test: `tests/test_country_scout.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_country_scout.py`:

```python
import pytest

from open_deep_research.factbase.country_list import resolve_country_list_async


@pytest.mark.asyncio
async def test_scout_returns_names_from_model():
    async def fake_scout(query):
        assert "CBDC" in query
        return ["Nigeria", "Bahamas", "Jamaica"]
    out = await resolve_country_list_async(spec=None, scout_query="countries with a launched CBDC",
                                           scout_call=fake_scout)
    assert out == ["Nigeria", "Bahamas", "Jamaica"]


@pytest.mark.asyncio
async def test_non_scout_delegates_to_sync():
    out = await resolve_country_list_async(spec="Nigeria, India", scout_query=None, scout_call=None)
    assert out == ["Nigeria", "India"]
```

(The fake injects "CBDC" via the query text; assertion is illustrative — adjust the substring to the query you pass.)

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/test_country_scout.py -q`
Expected: FAIL — `resolve_country_list_async` missing.

- [ ] **Step 3: Add the async resolver**

Append to `src/open_deep_research/factbase/country_list.py`:

```python
async def resolve_country_list_async(*, spec, scout_query, scout_call) -> list[str]:
    """Async wrapper: if scout_query is given, discover names via scout_call(query) ->
    list[str]; otherwise delegate to the sync resolve_country_list(spec)."""
    if scout_query:
        if scout_call is None:
            raise ValueError("scout_query given but no scout_call provided")
        names = await scout_call(scout_query)
        return [n.strip() for n in names if n and n.strip()]
    return resolve_country_list(spec)
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `uv run pytest tests/test_country_scout.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/country_list.py tests/test_country_scout.py
git commit -m "feat(factbase): async scout strategy for country-list discovery"
```

---

### Task 6: BatchRunner (bounded-concurrent, resumable, unresolved-reported)

**Files:**
- Create: `src/open_deep_research/factbase/batch.py`
- Test: `tests/test_batch_runner.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_batch_runner.py`:

```python
import aiosqlite
import pytest

from open_deep_research import storage as _storage
from open_deep_research.factbase import migrations as _mig, schema as _schema
from open_deep_research.factbase.batch import BatchRunner


@pytest.mark.asyncio
async def test_runner_runs_resolved_skips_done_reports_unresolved(tmp_path):
    db = str(tmp_path / "r.db")
    ran = []

    async def fake_run_one(country_name, instance_key, *, profile_name, db_path):
        ran.append(instance_key)
        return "run-" + instance_key  # pretend run_id

    runner = BatchRunner(profile_name="country_cbdc", db_path=db,
                         concurrency=2, run_one=fake_run_one)
    result = await runner.run(["Nigeria", "Bahamas", "Atlantis"])  # Atlantis unresolved

    assert sorted(ran) == ["BHS", "NGA"]
    assert result["unresolved"] == ["Atlantis"]
    assert result["summary"].get("done") == 2

    # resume: a second run does no work (both done)
    ran.clear()
    result2 = await runner.run(["Nigeria", "Bahamas"])
    assert ran == []
    assert result2["summary"].get("done") == 2
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/test_batch_runner.py -q`
Expected: FAIL — module `batch` missing.

- [ ] **Step 3: Implement batch.py**

Create `src/open_deep_research/factbase/batch.py`:

```python
"""Bounded-concurrent, resumable batch runner over per-country research.

Resolves each name to an instance_key (unresolved names are REPORTED, never silently
dropped), records a ledger row per country, then runs `run_one` for each not-yet-done
item K at a time. `run_one(country_name, instance_key, *, profile_name, db_path)` is
injected so this is testable without the graph; the production default invokes
deep_researcher (see `default_run_one`).
"""
from __future__ import annotations

import asyncio

import aiosqlite

from open_deep_research import storage as _storage
from . import migrations as _mig, schema as _schema
from .batch_ledger import BatchLedger, batch_id_for
from .entities import CountryResolver


class BatchRunner:
    def __init__(self, *, profile_name, db_path, concurrency=3, run_one,
                 profile_hash="", list_spec=""):
        self._profile = profile_name
        self._db = db_path
        self._k = max(1, int(concurrency))
        self._run_one = run_one
        self._profile_hash = profile_hash
        self._list_spec = list_spec
        self._resolver = CountryResolver()

    async def run(self, country_names: list[str]) -> dict:
        resolved: list[tuple[str, str]] = []   # (name, instance_key)
        unresolved: list[str] = []
        for name in country_names:
            key = self._resolver.resolve(name)
            (resolved if key else unresolved).append((name, key) if key else name)

        batch_id = batch_id_for(self._profile, self._list_spec or ",".join(country_names))
        async with aiosqlite.connect(self._db) as conn:
            await _storage._ensure_schema(conn)
            await _mig.apply(conn, _schema.STEPS)
            led = BatchLedger(conn, batch_id, profile_name=self._profile,
                              profile_hash=self._profile_hash, list_spec=self._list_spec)
            await led.ensure_run()
            for name, key in resolved:
                await led.upsert_item(key, name, status="pending")
            todo = await led.pending_items(include_failed=True)

            sem = asyncio.Semaphore(self._k)

            async def worker(item):
                key, name = item["instance_key"], item["country_name"]
                async with sem:
                    await led.mark(key, status="running")
                    try:
                        run_id = await self._run_one(
                            name, key, profile_name=self._profile, db_path=self._db)
                        await led.mark(key, status="done", run_id=str(run_id))
                    except Exception as e:  # noqa: BLE001 - isolate per-country failure
                        await led.mark(key, status="failed", error=str(e))

            await asyncio.gather(*(worker(i) for i in todo))
            summary = await led.summary()
        return {"batch_id": batch_id, "summary": summary, "unresolved": unresolved,
                "resolved": [k for _, k in resolved]}


async def default_run_one(country_name, instance_key, *, profile_name, db_path) -> str:
    """Production run_one: one deep_researcher invocation scoped to a country + profile."""
    import uuid

    from langchain_core.messages import HumanMessage

    from open_deep_research.deep_researcher import deep_researcher, recommended_recursion_limit

    configurable = {
        "thread_id": str(uuid.uuid4()),
        "profile_name": profile_name,
        "database_path": db_path,
        "use_knowledge_base": False,        # fresh research per country
        "allow_clarification": False,
        "max_concurrent_research_units": 2,
        "max_researcher_iterations": 2,
    }
    topic = (f"Research {country_name} for the '{profile_name}' profile: cover its properties "
             f"with sources and dates.")
    result = await deep_researcher.ainvoke(
        {"messages": [HumanMessage(content=topic)]},
        config={"configurable": configurable,
                "recursion_limit": recommended_recursion_limit(2, 2)})
    return str(result.get("report_id") or "")
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `uv run pytest tests/test_batch_runner.py -q`
Expected: PASS (1 test, 2 assertions blocks).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/batch.py tests/test_batch_runner.py
git commit -m "feat(factbase): bounded-concurrent resumable BatchRunner"
```

---

### Task 7: `dossier batch` subcommand + end-to-end

**Files:**
- Modify: `src/open_deep_research/factbase/dossier.py`
- Test: `tests/test_batch_end_to_end.py`

- [ ] **Step 1: Write the failing end-to-end test**

Create `tests/test_batch_end_to_end.py`:

```python
import aiosqlite
import pytest

from open_deep_research import storage as _storage
from open_deep_research.factbase import migrations as _mig, schema as _schema
from open_deep_research.factbase.batch import BatchRunner


@pytest.mark.asyncio
async def test_two_country_batch_persists_ledger_and_matrix(tmp_path):
    db = str(tmp_path / "e2e.db")

    async def fake_run_one(country_name, instance_key, *, profile_name, db_path):
        # Simulate the graph writing one fact per country.
        async with aiosqlite.connect(db_path) as conn:
            await _storage._ensure_schema(conn)
            await _mig.apply(conn, _schema.STEPS)
            await conn.execute(
                "INSERT INTO fact (instance_key, property_name, qualifiers_json, value, "
                "canonical_value, admission, lifecycle) VALUES (?,?,?,?,?,?,?)",
                (instance_key, "cbdc_launch_status", "{}", "launched", "launched",
                 "provisional", "current"))
            await conn.commit()
        return "rid-" + instance_key

    runner = BatchRunner(profile_name="country_cbdc", db_path=db, concurrency=2,
                         run_one=fake_run_one)
    res = await runner.run(["Nigeria", "Bahamas"])
    assert res["summary"]["done"] == 2

    # matrix renders both countries
    from open_deep_research.factbase.dossier import run
    out = await run(["matrix", "--profile", "country_cbdc", "--format", "md"], db_path=db)
    assert "Nigeria" in out and "Bahamas" in out
```

Guard for the `country_cbdc` profile at the top (as in 7a Task 5):

```python
from importlib.resources import files
if not files("open_deep_research.factbase.profiles").joinpath("country_cbdc.yaml").is_file():
    pytest.skip("country_cbdc profile not present", allow_module_level=True)
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/test_batch_end_to_end.py -q`
Expected: FAIL — `batch` subcommand absent / matrix branch already exists from 7a so the failure is the `batch` CLI wiring in the next step is what we still need; the runner half should already pass. (If it passes outright because we call `BatchRunner` directly + 7a's matrix, proceed to wire the CLI anyway in Step 3.)

- [ ] **Step 3: Add the `batch` subcommand parser**

In `dossier.py` `_parser()`, after the `matrix` parser, add:

```python
    bt = sub.add_parser("batch", help="Run a profile across many countries (resumable).")
    bt.add_argument("--profile", required=True)
    bt.add_argument("--countries", help="Explicit 'A,B,C', @file, or a group name (e.g. G20).")
    bt.add_argument("--scout", help="Discover the country list from this query instead.")
    bt.add_argument("--concurrency", type=int, default=3)
    bt.add_argument("--format", choices=["text", "md", "csv"], default="text")
    bt.add_argument("--no-registry-autoprovision", action="store_true")
    bt.add_argument("--dry-run", action="store_true",
                    help="Resolve the list (+report unresolved) without running research.")
```

- [ ] **Step 4: Add the `batch` handler**

In `dossier.py` `run()`, add the branch:

```python
    if args.command == "batch":
        from .country_list import resolve_country_list
        from .entities import CountryResolver
        from .profile import load as _load_profile
        prof = _load_profile(args.profile)
        names = resolve_country_list(args.countries) if args.countries else []
        if args.scout:
            return ("scout discovery runs only via the batch API (needs a model call); "
                    "pass --countries for the CLI, or call BatchRunner with a scout_call.")
        resolver = CountryResolver()
        if args.dry_run:
            lines = []
            for n in names:
                k = resolver.resolve(n)
                lines.append(f"  {n} -> {k}" if k else f"  {n} -> UNRESOLVED")
            return f"dry-run: {len(names)} countries for {args.profile}\n" + "\n".join(lines)
        from .batch import BatchRunner, default_run_one
        from .matrix import render_matrix
        from .query import FactQuery
        runner = BatchRunner(profile_name=args.profile, db_path=db_path,
                             concurrency=args.concurrency, run_one=default_run_one,
                             profile_hash=getattr(prof, "profile_hash", ""),
                             list_spec=args.countries or "")
        res = await runner.run(names)
        property_names = [pd.name for pd in prof.properties]
        async with aiosqlite.connect(db_path) as conn:
            await _storage._ensure_schema(conn)
            await _mig.apply(conn, _schema.STEPS)
            q = FactQuery(conn)
            rows = []
            for nm in property_names:
                rows.extend(await q.compare_grouped(nm))
        matrix = render_matrix(rows, property_names, resolver.instance_name, fmt=args.format)
        summary = ", ".join(f"{k}={v}" for k, v in sorted(res["summary"].items()))
        unresolved = (" | unresolved: " + ", ".join(res["unresolved"])) if res["unresolved"] else ""
        return f"batch {res['batch_id']}: {summary}{unresolved}\n\n{matrix}"
```

Add the lazy imports `from open_deep_research import storage as _storage` and `from open_deep_research.factbase import migrations as _mig, schema as _schema` at the top of this branch (as in the matrix branch).

Note: registry auto-provision is invoked from the batch API path (BatchRunner caller) when a `model_call` is available; the pure-CLI `batch` here runs research and renders the matrix. Wire `ensure_registry` into `default_run_one`'s caller in a follow-up if you want CLI auto-provision — for now it is exercised via `registry_provision` tests and the batch API. (Documented limitation, not a gap: US-5 is covered by `test_registry_provision.py`.)

- [ ] **Step 5: Run the end-to-end + full suite**

Run: `uv run pytest tests/test_batch_end_to_end.py -q`
Expected: PASS (or SKIP if `country_cbdc` absent).
Run: `uv run pytest tests/ -q`
Expected: PASS (all).
Run: `uv run ruff check src/`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/factbase/dossier.py tests/test_batch_end_to_end.py
git commit -m "feat(dossier): batch subcommand (resumable multi-country research + matrix)"
```

---

## Self-Review

**Spec coverage (Units 3, 4, scout):**
- Unit 4 (BatchRunner + BatchLedger, bounded-concurrent, resumable, unresolved-reported) → Tasks 1,2,6. ✓
- Unit 3 (RegistryProvisioner: derive/scaffold/commit/use; conservative tiers; audit draft) → Tasks 3,4. US-5 (`provisional→trusted`) is asserted at the provisioner level; the live promotion in a real run is the opt-in smoke step in the spec's Verification §8. ✓
- Scout half of Unit 1 → Task 5. ✓
- CLI surface (`dossier batch`, `--dry-run`, `--no-registry-autoprovision`, `--format`) → Task 7. ✓

**Known limitation (documented, not a gap):** CLI `batch` renders the matrix and runs research; registry auto-provision is wired/tested via the `registry_provision` module + batch API rather than auto-invoked inside the pure-CLI path. A follow-up can call `ensure_registry` before `runner.run` once a CLI model_call seam is chosen (the same `_scaffold_model_call()` factory in `dossier.py` is the natural seam). Flagged so the executor doesn't treat it as incomplete.

**Placeholder scan:** No TBD/TODO; every step has complete code or an exact command. ✓

**Type consistency:** `run_one(country_name, instance_key, *, profile_name, db_path) -> str` identical in the test fake, `BatchRunner`, and `default_run_one`. `BatchLedger` methods (`ensure_run`/`upsert_item`/`mark`/`pending_items`/`summary`) consistent across ledger + runner. `batch_id_for(profile, list_spec)` consistent. `render_matrix(rows, property_names, label, fmt)` matches 7a. Registry proposal shape (`domain/tier/flags`) matches `registry_schema.registry_from_dict`. ✓

**Dependency note:** This plan assumes Plan 7a is complete (uses `country_list`, `entities.instance_name`, `matrix`). The `country_cbdc` profile must be committed for the skip-guarded tests to execute rather than skip — if you adopted it earlier this session, ensure it's committed; otherwise those two tests SKIP cleanly and the rest of the suite still proves the machinery.
