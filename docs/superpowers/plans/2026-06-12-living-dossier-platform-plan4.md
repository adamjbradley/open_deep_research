# Living Fact Base — Implementation Plan (Plan 4 of N: Lifecycle Completion + Instrumentation)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Finish the v1 lifecycle and add the success-metric instrumentation: wire `finalize_research_run` so a run finalizes its preallocated row (no orphan/duplicate rows), add a stale-run reaper, model `required_qualifiers` correctly for promotion, derive `coverage_incomplete`, and expose coverage/groundedness metrics via `dossier stats`.

**Architecture:** Per Architecture v6 §6/§8 + the carried Plan-2/3 follow-ups. The big fix: `persist_research` currently INSERTs a NEW `research_runs` row (via `save_run_and_upsert_subject`/`log_research_run`) while `preallocate_run` already created one — leaving an orphan `running` row + a duplicate completed row every run. Plan 4 makes those storage functions UPDATE the preallocated row when given its id.

**Tech Stack:** Python 3.11, `aiosqlite`, `argparse`, `pytest` (`uv run pytest`; sync tests use `asyncio.run()`).

**Scope (v1 completion):** finalize wiring, reaper, `required_qualifiers`, `coverage_incomplete`, metrics + `dossier stats`. **Out of scope (v1.1):** the registry-version recompute pass (operational; the columns exist but no auto-recompute) and live-backend extraction verification (manual — can't unit-test a real LLM round-trip; tracked separately).

**Grounding (verified):** `save_run_and_upsert_subject` (storage.py:165) and `log_research_run` (storage.py:130) both INSERT into `research_runs`. `preallocate_run`/`finalize_research_run` exist (storage.py). `persist_research` (deep_researcher.py:905) calls them and has `state.get("prealloc_run_id")` available (set by the preallocate_run node). `PropertyDef` (profile.py) has `identity_qualifiers`; ingest computes `has_unspecified_required`. `FactQuery` (query.py) reads facts.

---

### Task 1: Finalize the preallocated run row (no orphan/duplicate)

**Files:**
- Modify: `src/open_deep_research/storage.py` (`save_run_and_upsert_subject`, `log_research_run`)
- Modify: `src/open_deep_research/deep_researcher.py` (`persist_research` passes `prealloc_run_id`)
- Test: `tests/test_run_finalize_wiring.py`

**Context:** Add an optional `run_id` param to both functions. When given (the preallocated id), UPDATE that row (set subject_id, topic, …, status) instead of INSERTing a new row; when `None`, INSERT as today (back-compat). `persist_research` passes `state.get("prealloc_run_id")`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_run_finalize_wiring.py
import asyncio, aiosqlite
from open_deep_research import storage
from open_deep_research.factbase import migrations, schema

def test_save_run_updates_preallocated_row_no_duplicate(tmp_path):
    db = str(tmp_path / "f.db")
    async def run():
        async with aiosqlite.connect(db) as conn:
            await storage._ensure_schema(conn)
            await migrations.apply(conn, schema.STEPS)
        rid = await storage.preallocate_run(db, "thread-1")
        run_doc = {"thread_id": "thread-1", "topic": "India DI", "research_brief": "b",
                   "final_report": "r", "sources": [], "raw_notes": [], "config": {},
                   "status": "completed", "error": None, "created_at": "2026-06-13T00:00:00Z"}
        sid, returned = await storage.save_run_and_upsert_subject(
            db, subject_name="India", slug="india", merged_report="r",
            sources_union=[], run=run_doc, now="2026-06-13T00:00:00Z", run_id=rid)
        assert returned == rid
        async with aiosqlite.connect(db) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT COUNT(*) AS n FROM research_runs")
            assert (await cur.fetchone())["n"] == 1            # UPDATED, not inserted a 2nd row
            cur = await conn.execute("SELECT status, topic, subject_id FROM research_runs WHERE id=?", (rid,))
            row = await cur.fetchone()
            assert row["status"] == "completed" and row["topic"] == "India DI" and row["subject_id"] == sid
    asyncio.run(run())

def test_save_run_without_run_id_still_inserts(tmp_path):
    db = str(tmp_path / "f.db")
    async def run():
        run_doc = {"thread_id": "t", "topic": "T", "research_brief": "", "final_report": "",
                   "sources": [], "raw_notes": [], "config": {}, "status": "completed",
                   "error": None, "created_at": "2026-06-13T00:00:00Z"}
        sid, rid = await storage.save_run_and_upsert_subject(
            db, subject_name="X", slug="x", merged_report="r", sources_union=[],
            run=run_doc, now="2026-06-13T00:00:00Z")  # no run_id -> insert
        assert isinstance(rid, int)
        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM research_runs")
            assert (await cur.fetchone())[0] == 1
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_run_finalize_wiring.py -v`
Expected: FAIL — `save_run_and_upsert_subject() got an unexpected keyword argument 'run_id'`.

- [ ] **Step 3: Implement**

In `storage.py`, change the signature of `save_run_and_upsert_subject` to add `run_id: Optional[int] = None` (keyword). Keep the subjects upsert + `dossier_versions` exactly as-is. Replace the run INSERT block with:
```python
        if run_id is not None:
            await conn.execute(
                """
                UPDATE research_runs SET
                    subject_id=?, thread_id=?, topic=?, research_brief=?, final_report=?,
                    sources=?, raw_notes=?, config=?, status=?, error=?
                WHERE id=?
                """,
                (subject_id, run.get("thread_id"), run.get("topic"), run.get("research_brief"),
                 run.get("final_report"), json.dumps(run.get("sources", [])),
                 json.dumps(run.get("raw_notes", [])), json.dumps(run.get("config", {})),
                 run.get("status", "completed"), run.get("error"), run_id),
            )
        else:
            run_cursor = await conn.execute(
                """
                INSERT INTO research_runs (
                    subject_id, thread_id, topic, research_brief, final_report,
                    sources, raw_notes, config, status, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (subject_id, run.get("thread_id"), run.get("topic"), run.get("research_brief"),
                 run.get("final_report"), json.dumps(run.get("sources", [])),
                 json.dumps(run.get("raw_notes", [])), json.dumps(run.get("config", {})),
                 run.get("status", "completed"), run.get("error"), run.get("created_at", now)),
            )
            run_id = run_cursor.lastrowid
```
(Then the `dossier_versions` INSERT and `return subject_id, run_id` stay; `run_id` is now always set.)

Do the same for `log_research_run`: add `run_id: Optional[int] = None`; when given, UPDATE that row's subject_id/thread_id/topic/.../status; else INSERT as today. Return `run_id`.

In `deep_researcher.py` `persist_research`: pass `run_id=state.get("prealloc_run_id")` to BOTH `save_run_and_upsert_subject(...)` and `log_research_run(...)` calls. (Find both call sites — the cache-hit path calls `log_research_run`, the research path calls `save_run_and_upsert_subject`.) `Optional` is imported in storage.py (it's used elsewhere) — confirm; add if missing.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_run_finalize_wiring.py tests/test_knowledge_flow.py -v`
Expected: PASS (the new tests + the existing persist tests in test_knowledge_flow.py, which exercise persist_research — they must still pass; with no prealloc_run_id in those test states, the INSERT path is used → back-compat).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/storage.py src/open_deep_research/deep_researcher.py tests/test_run_finalize_wiring.py
git commit -m "fix(factbase): finalize preallocated run row (no orphan/duplicate research_runs)"
```

---

### Task 2: Stale-`running` reaper

**Files:**
- Modify: `src/open_deep_research/storage.py` (add `reap_stale_running`)
- Test: `tests/test_reaper.py`

**Context:** A run that crashes before `persist_research` leaves a `status='running'` row (and its `run_source` rows). The reaper marks rows whose `last_heartbeat` is older than a TTL as `status='error'` (does NOT delete — keeps provenance; soft-delete of run_source is left to a later prune). Idempotent.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reaper.py
import asyncio, aiosqlite
from open_deep_research import storage
from open_deep_research.factbase import migrations, schema

def test_reaper_marks_old_running_as_error(tmp_path):
    db = str(tmp_path / "f.db")
    async def run():
        async with aiosqlite.connect(db) as conn:
            await storage._ensure_schema(conn)
            await migrations.apply(conn, schema.STEPS)
            # one stale running row, one fresh running row, one completed row
            await conn.executescript(
                "INSERT INTO research_runs (status, last_heartbeat) VALUES ('running','2000-01-01T00:00:00Z');"
                "INSERT INTO research_runs (status, last_heartbeat) VALUES ('running','2999-01-01T00:00:00Z');"
                "INSERT INTO research_runs (status, last_heartbeat) VALUES ('completed','2000-01-01T00:00:00Z');"
            )
            await conn.commit()
        n = await storage.reap_stale_running(db, older_than_iso="2026-06-13T00:00:00Z")
        assert n == 1   # only the stale running row
        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM research_runs WHERE status='running'")
            assert (await cur.fetchone())[0] == 1   # the future-dated running row survives
            cur = await conn.execute("SELECT COUNT(*) FROM research_runs WHERE status='error'")
            assert (await cur.fetchone())[0] == 1
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_reaper.py -v`
Expected: FAIL (`reap_stale_running` not defined).

- [ ] **Step 3: Implement (append to storage.py)**

```python
async def reap_stale_running(db_path: str, older_than_iso: str) -> int:
    """Mark stale 'running' runs (last_heartbeat < cutoff) as 'error'. Returns rows changed."""
    async with aiosqlite.connect(db_path) as conn:
        await _ensure_schema(conn)
        cur = await conn.execute(
            "UPDATE research_runs SET status='error', error='reaped: stale running run' "
            "WHERE status='running' AND last_heartbeat IS NOT NULL AND last_heartbeat < ?",
            (older_than_iso,),
        )
        await conn.commit()
        return cur.rowcount
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_reaper.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/storage.py tests/test_reaper.py
git commit -m "feat(factbase): stale-running run reaper"
```

---

### Task 3: `required_qualifiers` on PropertyDef → promotion gating

**Files:**
- Modify: `src/open_deep_research/factbase/profile.py` (add `required_qualifiers` field)
- Modify: `src/open_deep_research/factbase/profiles/country_digital_identity.py` (set required quals)
- Modify: `src/open_deep_research/factbase/ingest.py` (`has_unspecified_required` uses `required_qualifiers`)
- Test: `tests/test_factbase_profile.py` + `tests/test_factbase_ingest.py`

**Context:** Promotion should be blocked only when a *required* qualifier is unspecified — not every identity qualifier. For `id_coverage_pct`, `population_basis` is the required disambiguator; `coverage_kind`/`measured_modeled` are refinements.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_factbase_profile.py
def test_coverage_required_qualifiers_is_population_basis_only():
    cov = profile.load("country_digital_identity").property("id_coverage_pct")
    assert cov.required_qualifiers == ["population_basis"]
```
```python
# add to tests/test_factbase_ingest.py
def test_promotes_with_required_qualifier_even_if_refinements_missing():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            ing = _setup(conn)
            # supplies population_basis (required) but NOT coverage_kind/measured_modeled (refinements)
            recs = [{"property":"id_coverage_pct","instance_name":"India","value":"99","unit":"%","as_of":"2024",
                     "qualifiers":{"population_basis":"adults_15plus"},
                     "source_url":"https://id4d.worldbank.org/x","evidence_span":"99%"}]
            await ing.ingest(run_id=1, records=recs)
            cur = await conn.execute("SELECT admission FROM fact")
            assert (await cur.fetchone())[0] == "trusted"

def test_no_promote_when_required_qualifier_missing():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            ing = _setup(conn)
            # population_basis (required) MISSING -> stays provisional
            recs = [{"property":"id_coverage_pct","instance_name":"India","value":"99","unit":"%","as_of":"2024",
                     "qualifiers":{"coverage_kind":"enrolled"},
                     "source_url":"https://id4d.worldbank.org/x","evidence_span":"99%"}]
            await ing.ingest(run_id=1, records=recs)
            cur = await conn.execute("SELECT admission FROM fact")
            assert (await cur.fetchone())[0] == "provisional"
    asyncio.run(run())
```
(Wrap the first one in `asyncio.run` too — shown compact; make both real sync tests.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_factbase_profile.py tests/test_factbase_ingest.py -k "required" -v`
Expected: FAIL (`required_qualifiers` attribute missing).

- [ ] **Step 3: Implement**

In `profile.py` `PropertyDef`, add `required_qualifiers: list[str] = field(default_factory=list)`. (When empty, treat as "none required" — promotion not blocked on qualifiers.)

In `country_digital_identity.py`, set `required_qualifiers` on the relevant properties:
- `id_coverage_pct`: `required_qualifiers=["population_basis"]`
- `scheme_status`: `required_qualifiers=["basis"]`
- `data_protection_law`: `required_qualifiers=["stage"]`
- others: leave default `[]`.
(Add the kwarg to each PropertyDef(...) call.)

In `ingest.py`, change the `has_unspecified_required` computation from "any/all identity qualifier is None" to: **any *required* qualifier is None**:
```python
        req = getattr(pd, "required_qualifiers", []) or []
        has_unspec = any(quals.get(q) is None for q in req)
```
(`quals` already = `{q: rec.qualifiers.get(q) for q in pd.identity_qualifiers}`; required ⊆ identity, so `quals.get(q)` is valid. If a required qualifier isn't even in identity_qualifiers, fall back to `rec.get("qualifiers", {}).get(q)`.)

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_factbase_profile.py tests/test_factbase_ingest.py -v`
Expected: PASS (including the prior ingest tests — note `test_single_trusted_source_promotes` supplies `population_basis`, so it still promotes).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/profile.py src/open_deep_research/factbase/profiles/country_digital_identity.py src/open_deep_research/factbase/ingest.py tests/test_factbase_profile.py tests/test_factbase_ingest.py
git commit -m "feat(factbase): required_qualifiers gate promotion (not every identity qualifier)"
```

---

### Task 4: Metrics + `dossier stats`

**Files:**
- Create: `src/open_deep_research/factbase/metrics.py`
- Modify: `src/open_deep_research/factbase/query.py` (a small all-facts read for metrics) OR compute in metrics.py via its own query
- Modify: `src/open_deep_research/factbase/dossier.py` (add `stats` subcommand)
- Test: `tests/test_factbase_metrics.py`

**Context:** The §3 success metrics: coverage (# instances with ≥1 trusted fact), groundedness (% facts with a profile-trusted source — i.e. source_tier in {reputable,authoritative}), trusted/provisional counts, open-conflict count. Plus the **anti-metric note** in the docstring: never optimize raw fact count.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_metrics.py
import asyncio, aiosqlite
from open_deep_research.factbase import migrations, schema, ingest, profile, entities, registry, metrics

DI = profile.load("country_digital_identity")

def test_metrics_counts(tmp_path):
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            ing = ingest.Ingestor(conn, profile=DI, resolver=entities.CountryResolver(),
                                  registry=registry.SourceRegistry.load("di_source_registry"))
            # India: one trusted (id4d, no conflict); Estonia: two conflicting (both provisional)
            await ing.ingest(run_id=1, records=[
                {"property":"id_coverage_pct","instance_name":"India","value":"99","unit":"%","as_of":"2024",
                 "qualifiers":{"population_basis":"adults_15plus"},"source_url":"https://id4d.worldbank.org/x","evidence_span":"99%"},
                {"property":"id_coverage_pct","instance_name":"Estonia","value":"95","unit":"%","as_of":"2024",
                 "qualifiers":{"population_basis":"adults_15plus"},"source_url":"https://id4d.worldbank.org/e","evidence_span":"95%"},
                {"property":"id_coverage_pct","instance_name":"Estonia","value":"88","unit":"%","as_of":"2024",
                 "qualifiers":{"population_basis":"adults_15plus"},"source_url":"https://gsma.com/e","evidence_span":"88%"},
            ])
            m = await metrics.compute(conn)
            assert m["total_facts"] == 3
            assert m["trusted_facts"] == 1            # only India's (Estonia's two are in conflict)
            assert m["instances_with_trusted"] == 1   # India
            assert m["open_conflicts"] == 1           # Estonia
            assert 0.0 <= m["groundedness"] <= 1.0
            assert m["groundedness"] == 1.0           # all 3 sources are registry-tier (id4d/gsma authoritative)
    asyncio.run(run())
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_factbase_metrics.py -v`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement**

```python
# src/open_deep_research/factbase/metrics.py
"""Fact-base health metrics (Feature Spec §4).

ANTI-METRIC: total_fact_count is reported for visibility but must NEVER be an
optimization target — volume without groundedness is the failure mode.
"""
from __future__ import annotations
import aiosqlite


async def compute(conn: aiosqlite.Connection) -> dict:
    async def scalar(sql: str, params: tuple = ()) -> int:
        cur = await conn.execute(sql, params)
        return (await cur.fetchone())[0]

    total = await scalar("SELECT COUNT(*) FROM fact WHERE soft_deleted_at IS NULL")
    trusted = await scalar("SELECT COUNT(*) FROM fact WHERE admission='trusted' AND soft_deleted_at IS NULL")
    provisional = total - trusted
    instances_with_trusted = await scalar(
        "SELECT COUNT(DISTINCT instance_key) FROM fact WHERE admission='trusted' AND soft_deleted_at IS NULL")
    open_conflicts = await scalar("SELECT COUNT(*) FROM conflict WHERE status='open'")
    grounded = await scalar(
        "SELECT COUNT(*) FROM fact f JOIN source s ON s.id=f.source_id "
        "WHERE f.soft_deleted_at IS NULL AND s.tier IN ('reputable','authoritative')")
    return {
        "total_facts": total,                      # ANTI-METRIC: never optimize
        "trusted_facts": trusted,
        "provisional_facts": provisional,
        "instances_with_trusted": instances_with_trusted,
        "open_conflicts": open_conflicts,
        "groundedness": (grounded / total) if total else 0.0,
    }
```

Add a `stats` subcommand to `dossier.py`: in `_parser()` add `sub.add_parser("stats", ...)`; in `run()`, `if args.cmd == "stats":` open the conn and return a formatted block of `await metrics.compute(conn)` (one `key: value` per line). Import `from . import metrics as _metrics`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_factbase_metrics.py tests/test_dossier_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/metrics.py src/open_deep_research/factbase/dossier.py tests/test_factbase_metrics.py
git commit -m "feat(factbase): health metrics + dossier stats (anti-metric: raw count)"
```

---

### Task 5: Derive `coverage_incomplete` when sources are skipped

**Files:**
- Modify: `src/open_deep_research/deep_researcher.py` (`extract_facts` sets coverage_incomplete via finalize fields, or persist_research finalizes with it)
- Test: `tests/test_coverage_incomplete.py`

**Context:** When `extract_facts` skips any non-`raw_text` source (summarizing adapter) or hits a ceiling, the run's `coverage_incomplete` should be set. Simplest: `extract_facts` computes a boolean (any run_source row with `capture_status != 'raw_text'`) and persists it on the preallocated run row directly via a tiny storage helper `set_coverage_incomplete(db_path, run_id, value)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_coverage_incomplete.py
import asyncio, aiosqlite
from open_deep_research import storage
from open_deep_research.factbase import migrations, schema, store

def test_set_coverage_incomplete(tmp_path):
    db = str(tmp_path / "f.db")
    async def run():
        async with aiosqlite.connect(db) as conn:
            await storage._ensure_schema(conn)
            await migrations.apply(conn, schema.STEPS)
        rid = await storage.preallocate_run(db, "t1")
        await storage.set_coverage_incomplete(db, rid, True)
        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute("SELECT coverage_incomplete FROM research_runs WHERE id=?", (rid,))
            assert (await cur.fetchone())[0] == 1
    asyncio.run(run())
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_coverage_incomplete.py -v`
Expected: FAIL (`set_coverage_incomplete` not defined).

- [ ] **Step 3: Implement**

Append to `storage.py`:
```python
async def set_coverage_incomplete(db_path: str, run_id: int, value: bool) -> None:
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("UPDATE research_runs SET coverage_incomplete=? WHERE id=?",
                           (1 if value else 0, run_id))
        await conn.commit()
```

In `deep_researcher.py` `extract_facts`, after reading `sources`, compute and persist:
```python
            skipped = any(s["capture_status"] != "raw_text" for s in sources)
            if run_id and skipped:
                from open_deep_research.storage import set_coverage_incomplete
                await set_coverage_incomplete(get_db_path(config), run_id, True)
```
(Place it inside the existing try/except so it stays best-effort.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_coverage_incomplete.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/storage.py src/open_deep_research/deep_researcher.py tests/test_coverage_incomplete.py
git commit -m "feat(factbase): derive coverage_incomplete when sources are skipped"
```

---

### Task 6: Full-suite check

- [ ] **Step 1:** Run `uv run pytest -q -p no:warnings` → all pass.
- [ ] **Step 2:** `uv run python -c "import open_deep_research.deep_researcher; print('compiles')"` → `compiles`.
- [ ] **Step 3:** `uv run python -c "import open_deep_research.factbase.dossier as d; d._parser().parse_args(['stats'])"` → no error.
- [ ] **Step 4:** (no commit unless a fix was needed)

---

## Plan 4 complete → v1 functionally complete

After Task 6: the run lifecycle is whole (preallocate → finalize, one row per run; a reaper cleans crashes), promotion is gated on *required* qualifiers, `coverage_incomplete` is recorded, and `dossier stats` reports coverage/groundedness/trusted-ratio/open-conflicts with the raw-count anti-metric flagged.

**Remaining (v1.1 / manual, tracked):**
- **Live extraction verification** — run the graph against a real Claude/Gemini/Codex backend end-to-end (the `_make_fact_model_call` round-trip is only stub-tested) and calibrate the "explicitly states; else omit" extraction prompt against the false-conflict / drop-rate metrics. This is the highest-value real-world validation.
- **Registry-version recompute pass** (forward-only, atomic, resumable) — the columns/flags exist; auto-recompute on tier change is deferred.
- **Cosmetic Plan-1/2/3 minors:** AutoClose per-group `as_of`; `trust_threshold` REAL↔str; dataclass↔pydantic; `row_factory` side effects; render value+unit separator; `compare()` instance ordering; per-qualifier comparison columns (FS AC4.1).
