# Living Fact Base — Implementation Plan (Plan 2 of N: Extraction & Graph Hook)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a real research run actually produce facts — persist per-source text, extract qualifier-aware facts from it, gate/conflict them, and write them to the fact base — by wiring the (already-built, merged) `factbase` foundation into the running `deep_researcher` graph.

**Architecture:** Per `docs/superpowers/specs/2026-06-12-living-dossier-platform-architecture.md` (v6) §11 steps 5–6. Source text reaches extraction via a `run_source` side store written at the search-tool layer (keyed by `thread_id`, the only stable run key available there). A single post-supervisor `extract_facts` node runs the `FactExtractor` once per source, validates/span-verifies, and hands candidates to a pure-orchestration **ingestion service** that uses `FactWriter` to persist in one transaction. The run lifecycle is preallocated at graph start and finalized in `persist_research`.

**Tech Stack:** Python 3.11, `aiosqlite`, `pydantic`, LangGraph, `pytest` (`uv run pytest`; repo pattern: `asyncio.run()` in sync tests, NOT `@pytest.mark.asyncio`).

**Scope:** v1 targets the **Tavily** search backend (the one that exposes per-source `raw_content`, utils.py:75/85). Summarizing CLI/native search backends record a `summarized` `run_source` row (no text) → those sources are skipped by extraction and the run is flagged `coverage_incomplete` (no facts, by design). The `dossier` CLI is **Plan 3**; instrumentation/recompute/reaper is **Plan 4**.

**Grounding (verified against the code):**
- `tavily_search` (utils.py:52) has `config: RunnableConfig`; `unique_results[url]` holds raw `raw_content` (:80–85) before summarization. `Configuration.from_runnable_config(config)` + `config["configurable"]["thread_id"]` are available there.
- `persist_research` (deep_researcher.py:905) is the single node BOTH terminal paths reach (`answer_from_dossier → persist_research`, `final_report_generation → persist_research`; edges :1041/:1044). `run_id` is `lastrowid` from the INSERT in `storage.save_run_and_upsert_subject`/`log_research_run` (storage.py).
- Graph nodes/edges are registered at deep_researcher.py ~:1030–1045. `AgentState` (state.py:92) has no fact field; `notes` is cleared by `final_report_generation`; `raw_notes` survives but is a flattened string.
- `factbase` package (merged): `migrations.apply`, `schema.STEPS`, `identity.{canonicalize,values_equal,tuple_key}`, `model.{Fact,Promote,Demote,OpenConflict,AutoClose}`, `conflict.detect`, `promotion.evaluate`, `profile.load`, `entities.CountryResolver`.

**New data types (Task 4 / Task 6):** `RunSourceStore` (async), `SourceRegistry` (sync, data-file), `FactExtractor` (async, model-injected), `FactWriter` (async). Defined in their tasks.

---

### Task 1: Migration v2 — run_source.thread_id + research_runs lifecycle columns

**Files:**
- Modify: `src/open_deep_research/factbase/schema.py` (append a version-2 step)
- Test: `tests/test_factbase_schema.py` (add a test)

**Context:** `run_source` (Plan 1) keys by `run_id`, but the tool layer only has `thread_id`. Add `thread_id` + a content-hash uniqueness aid, and add lifecycle columns to the legacy `research_runs` table (created by `storage._SCHEMA`). The migration runs against the same DB after `storage` has ensured its schema (Task 8 wires the ordering).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_factbase_schema.py
def test_v2_adds_thread_id_and_run_lifecycle_columns():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            # research_runs exists first (legacy storage table) so the ALTERs apply
            await conn.executescript(
                "CREATE TABLE research_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT);"
            )
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            cur = await conn.execute("PRAGMA table_info(run_source)")
            rs_cols = {r[1] for r in await cur.fetchall()}
            assert "thread_id" in rs_cols
            cur = await conn.execute("PRAGMA table_info(research_runs)")
            rr_cols = {r[1] for r in await cur.fetchall()}
            assert {"status", "coverage_incomplete", "last_heartbeat"} <= rr_cols
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_factbase_schema.py::test_v2_adds_thread_id_and_run_lifecycle_columns -v`
Expected: FAIL (no `thread_id` column).

- [ ] **Step 3: Implement — append a version-2 step to `schema.STEPS`**

```python
# append inside the STEPS list in src/open_deep_research/factbase/schema.py
    (2, """
    ALTER TABLE run_source ADD COLUMN thread_id TEXT;
    CREATE INDEX IF NOT EXISTS ix_run_source_thread ON run_source(thread_id);
    ALTER TABLE research_runs ADD COLUMN status TEXT;
    ALTER TABLE research_runs ADD COLUMN coverage_incomplete INTEGER DEFAULT 0;
    ALTER TABLE research_runs ADD COLUMN last_heartbeat TEXT;
    """),
```

(The migration framework splits on `;` and runs each statement in one transaction — no embedded semicolons here, so it is safe.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_factbase_schema.py -v`
Expected: PASS (all schema tests).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/schema.py tests/test_factbase_schema.py
git commit -m "feat(factbase): migration v2 — run_source.thread_id + research_runs lifecycle cols"
```

---

### Task 2: SourceRegistry (data-file adapter)

**Files:**
- Create: `src/open_deep_research/factbase/registry.py`
- Create: `src/open_deep_research/factbase/profiles/di_source_registry.py`
- Test: `tests/test_factbase_registry.py`

**Context:** Resolves a source URL → trust tier for a (type, property), with domain corrections (ID4D=modeled; national operators not above academics for coverage). Data lives in the profile package; the adapter is a thin lookup. `meets_bar(url, threshold)` answers the promotion gate.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_registry.py
from open_deep_research.factbase import registry

def test_known_domain_resolves_to_tier():
    r = registry.SourceRegistry.load("di_source_registry")
    assert r.tier("https://id4d.worldbank.org/data") == "authoritative"
    assert r.tier("https://some-random-blog.example/post") == "unvetted"

def test_meets_bar_orders_tiers():
    r = registry.SourceRegistry.load("di_source_registry")
    assert r.meets_bar("https://id4d.worldbank.org/x", "reputable") is True   # authoritative >= reputable
    assert r.meets_bar("https://some-random-blog.example/p", "reputable") is False

def test_modeled_flag_surfaced():
    r = registry.SourceRegistry.load("di_source_registry")
    assert "modeled" in r.flags("https://id4d.worldbank.org/data")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_factbase_registry.py -v`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement**

```python
# src/open_deep_research/factbase/registry.py
from __future__ import annotations
import importlib
from urllib.parse import urlparse

_TIER_RANK = {"unvetted": 0, "reputable": 1, "authoritative": 2}


class SourceRegistry:
    def __init__(self, entries: dict[str, dict]):
        # entries keyed by registrable domain suffix -> {"tier":..., "flags":[...]}
        self._entries = entries

    @classmethod
    def load(cls, name: str) -> "SourceRegistry":
        mod = importlib.import_module(f"open_deep_research.factbase.profiles.{name}")
        return cls(mod.ENTRIES)

    def _match(self, url: str) -> dict | None:
        host = (urlparse(url).hostname or "").lower()
        for domain, entry in self._entries.items():
            if host == domain or host.endswith("." + domain):
                return entry
        return None

    def tier(self, url: str) -> str:
        m = self._match(url)
        return m["tier"] if m else "unvetted"

    def flags(self, url: str) -> list[str]:
        m = self._match(url)
        return list(m.get("flags", [])) if m else []

    def meets_bar(self, url: str, threshold: str) -> bool:
        return _TIER_RANK[self.tier(url)] >= _TIER_RANK[threshold]
```

```python
# src/open_deep_research/factbase/profiles/di_source_registry.py
"""Curated DI source registry (Architecture §6). Editable data, not code."""
ENTRIES: dict[str, dict] = {
    "id4d.worldbank.org": {"tier": "authoritative", "flags": ["modeled"]},
    "worldbank.org": {"tier": "authoritative", "flags": ["modeled"]},
    "gsma.com": {"tier": "authoritative", "flags": []},
    "mosip.io": {"tier": "reputable", "flags": []},
    # national operators: reputable but NOT above academic for coverage (flagged incentivized)
    "uidai.gov.in": {"tier": "reputable", "flags": ["incentivized"]},
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_factbase_registry.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/registry.py src/open_deep_research/factbase/profiles/di_source_registry.py tests/test_factbase_registry.py
git commit -m "feat(factbase): curated SourceRegistry (tier/flags/meets_bar)"
```

---

### Task 3: RunSourceStore (async aiosqlite adapter)

**Files:**
- Create: `src/open_deep_research/factbase/store.py`
- Test: `tests/test_factbase_store.py`

**Context:** Writes/reads per-source rows keyed by `thread_id`. Content-hash de-duplicates re-fetched URLs within a thread. `capture_status` records whether raw text is available.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_store.py
import asyncio, aiosqlite
from open_deep_research.factbase import migrations, schema, store

def _db():
    # in-memory shared connection helper: tests open their own connection
    return ":memory:"

def test_record_and_read_run_sources():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            s = store.RunSourceStore(conn)
            await s.record("thread-1", "https://x.org/a", "RAW TEXT A", capture_status="raw_text")
            await s.record("thread-1", "https://x.org/a", "RAW TEXT A", capture_status="raw_text")  # dup -> no second row
            await s.record("thread-1", "https://y.org/b", None, capture_status="summarized")
            rows = await s.read("thread-1")
            urls = sorted(r["source_url"] for r in rows)
            assert urls == ["https://x.org/a", "https://y.org/b"]   # dedup kept a single 'a'
            raw = [r for r in rows if r["capture_status"] == "raw_text"]
            assert raw[0]["text"] == "RAW TEXT A"
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_factbase_store.py -v`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement**

```python
# src/open_deep_research/factbase/store.py
from __future__ import annotations
import hashlib
from datetime import datetime, timezone
import aiosqlite


def _hash(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


class RunSourceStore:
    """Per-source text captured during a run, keyed by thread_id."""

    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    async def record(self, thread_id: str, url: str, text: str | None, *, capture_status: str) -> None:
        ch = _hash(text)
        cur = await self._conn.execute(
            "SELECT 1 FROM run_source WHERE thread_id=? AND source_url=? AND content_hash=?",
            (thread_id, url, ch),
        )
        if await cur.fetchone():
            return  # content-hash dedup within the thread
        await self._conn.execute(
            "INSERT INTO run_source (thread_id, source_url, capture_status, text, content_hash, retrieved_at) "
            "VALUES (?,?,?,?,?,?)",
            (thread_id, url, capture_status, text, ch, datetime.now(timezone.utc).isoformat()),
        )
        await self._conn.commit()

    async def read(self, thread_id: str) -> list[dict]:
        self._conn.row_factory = aiosqlite.Row
        cur = await self._conn.execute(
            "SELECT id, source_url, capture_status, text FROM run_source "
            "WHERE thread_id=? AND soft_deleted_at IS NULL",
            (thread_id,),
        )
        return [dict(r) for r in await cur.fetchall()]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_factbase_store.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/store.py tests/test_factbase_store.py
git commit -m "feat(factbase): RunSourceStore (thread-keyed, content-hash dedup)"
```

---

### Task 4: Hook tavily_search to record run_source

**Files:**
- Modify: `src/open_deep_research/utils.py` (in `tavily_search`, after `unique_results` is built, ~line 86)
- Test: `tests/test_tavily_run_source.py`

**Context:** Persist each unique result's `raw_content` as a `raw_text` run_source row, keyed by the run's `thread_id`. Best-effort: a storage failure must NOT break search (matches the repo's tolerant persistence style). Guard with `Configuration.use_knowledge_base`-style flag — reuse `persist_results`.

- [ ] **Step 1: Write the failing test** (unit-test the helper, not the whole tool)

Add a small helper `record_search_sources` so it's testable without calling Tavily.

```python
# tests/test_tavily_run_source.py
import asyncio, aiosqlite
from open_deep_research.factbase import migrations, schema, store
from open_deep_research import utils

def test_record_search_sources_writes_raw_rows():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            unique = {
                "https://a.org/1": {"raw_content": "RAW A"},
                "https://b.org/2": {"raw_content": ""},  # empty -> summarized stub, no text
            }
            await utils.record_search_sources(store.RunSourceStore(conn), "thread-9", unique)
            rows = await store.RunSourceStore(conn).read("thread-9")
            by_url = {r["source_url"]: r for r in rows}
            assert by_url["https://a.org/1"]["capture_status"] == "raw_text"
            assert by_url["https://a.org/1"]["text"] == "RAW A"
            assert by_url["https://b.org/2"]["capture_status"] == "summarized"
            assert by_url["https://b.org/2"]["text"] is None
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tavily_run_source.py -v`
Expected: FAIL (`record_search_sources` not defined).

- [ ] **Step 3: Implement the helper + call it from `tavily_search`**

Add near the top of utils.py (after imports):

```python
async def record_search_sources(run_source_store, thread_id: str, unique_results: dict) -> None:
    """Persist each unique search result as a run_source row (raw_text if raw_content present)."""
    for url, result in unique_results.items():
        raw = (result or {}).get("raw_content") or ""
        if raw:
            await run_source_store.record(thread_id, url, raw, capture_status="raw_text")
        else:
            await run_source_store.record(thread_id, url, None, capture_status="summarized")
```

In `tavily_search`, immediately after the `unique_results` loop (after line 85), add a best-effort call:

```python
    # Persist per-source raw text for fact extraction (best-effort; never break search).
    try:
        from open_deep_research.factbase import store as _fb_store
        from open_deep_research.storage import get_db_path as _get_db_path
        import aiosqlite as _aiosqlite
        _configurable = (config or {}).get("configurable", {}) if config else {}
        _thread_id = _configurable.get("thread_id")
        if _thread_id and Configuration.from_runnable_config(config).persist_results:
            async with _aiosqlite.connect(_get_db_path(config)) as _conn:
                await record_search_sources(_fb_store.RunSourceStore(_conn), str(_thread_id), unique_results)
    except Exception as _e:
        logger.warning("run_source capture failed (non-fatal): %s", _e)
```

(`logger` already exists in utils.py; `Configuration` is imported there.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tavily_run_source.py -v`
Expected: PASS.

- [ ] **Step 5: Run full suite + commit**

```bash
uv run pytest -q -p no:warnings   # expect all pass
git add src/open_deep_research/utils.py tests/test_tavily_run_source.py
git commit -m "feat(factbase): capture per-source raw text to run_source in tavily_search"
```

---

### Task 5: FactExtractor (one call per source, span-verified, abstaining)

**Files:**
- Create: `src/open_deep_research/factbase/extractor.py`
- Test: `tests/test_factbase_extractor.py`

**Context:** Given one source's text + the profile, ask the model (structured output) for a flat list of `FactRecord`. The model is **injected** (a callable returning the structured result) so the extractor is unit-tested with a stub — no live LLM in tests. Post-coercion validation: drop records whose `evidence_span` is not a (whitespace-normalized) substring of the source text, or that fail `property_def.validate`, or whose property/qualifier enums are unknown.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_extractor.py
from open_deep_research.factbase import extractor, profile

DI = profile.load("country_digital_identity")

def _raw(records):
    # stub model: returns whatever records we inject, ignoring prompt
    async def _call(source_text, prof):
        return records
    return _call

def test_keeps_valid_span_verified_record():
    import asyncio
    rec = {"property": "id_coverage_pct", "instance_name": "India", "value": "99", "unit": "%",
           "as_of": "2024", "qualifiers": {"population_basis": "adults_15plus"},
           "evidence_span": "coverage reached 99%"}
    out = asyncio.run(extractor.extract("India coverage reached 99% in 2024", DI, _raw([rec])))
    assert len(out) == 1 and out[0]["value"] == "99"

def test_drops_unverifiable_span():
    import asyncio
    rec = {"property": "id_coverage_pct", "instance_name": "India", "value": "42", "unit": "%",
           "as_of": "2024", "qualifiers": {"population_basis": "adults_15plus"},
           "evidence_span": "coverage was 42%"}  # NOT in source text -> dropped
    out = asyncio.run(extractor.extract("India coverage reached 99% in 2024", DI, _raw([rec])))
    assert out == []

def test_drops_value_failing_validation():
    import asyncio
    rec = {"property": "id_coverage_pct", "instance_name": "India", "value": "412", "unit": "%",
           "as_of": "2024", "qualifiers": {}, "evidence_span": "412"}
    out = asyncio.run(extractor.extract("nonsense 412", DI, _raw([rec])))
    assert out == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_factbase_extractor.py -v`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement**

```python
# src/open_deep_research/factbase/extractor.py
"""Per-source fact extraction with post-coercion validation + span verification.

`model_call(source_text, profile) -> list[dict]` is injected so this is unit-testable
without a live LLM. Records that fail validation/span checks are dropped (abstain).
"""
from __future__ import annotations
import re
from .profile import Profile

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").strip().lower())


async def extract(source_text: str, prof: Profile, model_call) -> list[dict]:
    raw = await model_call(source_text, prof)
    norm_source = _norm(source_text)
    kept: list[dict] = []
    for rec in raw or []:
        try:
            pd = prof.property(rec["property"])
        except KeyError:
            continue
        span = rec.get("evidence_span", "")
        if not span or _norm(span) not in norm_source:
            continue  # span not verbatim in source -> drop
        if not pd.validate(rec.get("value", "")):
            continue
        # qualifier enum check: unknown enum value -> drop that record
        ok = True
        for q, v in (rec.get("qualifiers") or {}).items():
            if v is None:
                continue
            allowed = pd.qualifier_enums.get(q)
            if allowed is not None and v.lower() not in {a.lower() for a in allowed}:
                ok = False
                break
        if ok:
            kept.append(rec)
    return kept
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_factbase_extractor.py -v`
Expected: PASS (all 3).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/extractor.py tests/test_factbase_extractor.py
git commit -m "feat(factbase): FactExtractor validation + span verification (model injected)"
```

---

### Task 6: FactWriter + ingestion service (atomic, per-fact isolation)

**Files:**
- Create: `src/open_deep_research/factbase/ingest.py`
- Test: `tests/test_factbase_ingest.py`

**Context:** Pure orchestration: for a list of validated `FactRecord` dicts (one thread/run), resolve instance (CountryResolver; miss → `unresolved_instance`), compute `tuple_key`, look up source tier (SourceRegistry → `source_meets_bar`), build `model.Fact`s, group into (tuple_key, as_of) buckets, run `conflict.detect` + `promotion.evaluate`, and write facts + evidence + revisions + conflicts in ONE transaction with per-fact isolation. Dedup on `(tuple_key, as_of, canonical(value,unit), source_id)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_ingest.py
import asyncio, aiosqlite
from open_deep_research.factbase import migrations, schema, ingest, profile, entities, registry

DI = profile.load("country_digital_identity")

def _setup(conn):
    return ingest.Ingestor(conn, profile=DI, resolver=entities.CountryResolver(),
                            registry=registry.SourceRegistry.load("di_source_registry"))

def test_two_conflicting_trust_bar_facts_open_conflict_and_stay_provisional():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            ing = _setup(conn)
            recs = [
                {"property":"id_coverage_pct","instance_name":"India","value":"99","unit":"%","as_of":"2024",
                 "qualifiers":{"population_basis":"adults_15plus"},"source_url":"https://id4d.worldbank.org/x","evidence_span":"99%"},
                {"property":"id_coverage_pct","instance_name":"India","value":"87","unit":"%","as_of":"2024",
                 "qualifiers":{"population_basis":"adults_15plus"},"source_url":"https://gsma.com/y","evidence_span":"87%"},
            ]
            await ing.ingest(run_id=1, records=recs)
            cur = await conn.execute("SELECT COUNT(*) FROM fact"); assert (await cur.fetchone())[0] == 2
            cur = await conn.execute("SELECT COUNT(*) FROM conflict WHERE status='open'"); assert (await cur.fetchone())[0] == 1
            cur = await conn.execute("SELECT COUNT(*) FROM fact WHERE admission='trusted'"); assert (await cur.fetchone())[0] == 0
            cur = await conn.execute("SELECT COUNT(*) FROM evidence"); assert (await cur.fetchone())[0] == 2
    asyncio.run(run())

def test_single_trusted_source_promotes():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            ing = _setup(conn)
            recs = [{"property":"id_coverage_pct","instance_name":"India","value":"99","unit":"%","as_of":"2024",
                     "qualifiers":{"population_basis":"adults_15plus"},"source_url":"https://id4d.worldbank.org/x","evidence_span":"99%"}]
            await ing.ingest(run_id=1, records=recs)
            cur = await conn.execute("SELECT admission FROM fact"); assert (await cur.fetchone())[0] == "trusted"
    asyncio.run(run())

def test_unresolved_instance_quarantined_not_a_fact():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            ing = _setup(conn)
            recs = [{"property":"id_coverage_pct","instance_name":"Atlantis","value":"50","unit":"%","as_of":"2024",
                     "qualifiers":{"population_basis":"adults_15plus"},"source_url":"https://id4d.worldbank.org/x","evidence_span":"50%"}]
            await ing.ingest(run_id=1, records=recs)
            cur = await conn.execute("SELECT COUNT(*) FROM fact"); assert (await cur.fetchone())[0] == 0
            cur = await conn.execute("SELECT COUNT(*) FROM unresolved_instance"); assert (await cur.fetchone())[0] == 1
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_factbase_ingest.py -v`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement**

```python
# src/open_deep_research/factbase/ingest.py
"""Ingestion orchestration: records -> resolve/identity/registry -> conflict/promotion -> atomic write."""
from __future__ import annotations
from datetime import datetime, timezone
import json
import aiosqlite

from . import identity, model, conflict, promotion


def _trusted_threshold(pd) -> str:
    return getattr(pd, "trust_threshold", "reputable")


class Ingestor:
    def __init__(self, conn: aiosqlite.Connection, *, profile, resolver, registry):
        self._conn = conn
        self._profile = profile
        self._resolver = resolver
        self._registry = registry

    async def ingest(self, *, run_id: int, records: list[dict]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        # Build candidate Facts (resolve + identity + registry); quarantine misses.
        candidates = []  # (rec, Fact-without-id, source_id, property_def)
        await self._conn.execute("BEGIN")
        try:
            for rec in records:
                pd = self._profile.property(rec["property"])
                instance_key = self._resolver.resolve(rec.get("instance_name", ""))
                if instance_key is None:
                    await self._conn.execute(
                        "INSERT INTO unresolved_instance (raw_name, run_id, created_at) VALUES (?,?,?)",
                        (rec.get("instance_name"), run_id, now),
                    )
                    continue
                quals = {q: rec.get("qualifiers", {}).get(q) for q in pd.identity_qualifiers}
                tk = identity.tuple_key(abs(hash(instance_key)) % (10**9), pd.name, quals)
                url = rec.get("source_url", "")
                source_id = await self._source_id(url, now)
                meets_bar = self._registry.meets_bar(url, _trusted_threshold(pd))
                has_unspec = any(quals.get(q) is None for q in pd.identity_qualifiers)
                as_of = int(rec["as_of"]) if str(rec.get("as_of", "")).isdigit() else None
                f = model.Fact(fact_id=None, tuple_key=tk, as_of=as_of, value=rec["value"],
                               unit=rec.get("unit"), source_meets_bar=meets_bar,
                               has_unspecified_required=has_unspec)
                candidates.append((rec, f, source_id, instance_key))

            # Group by (tuple_key, as_of), detect conflicts, then insert facts + evidence.
            buckets: dict[tuple, list] = {}
            for rec, f, sid, _ in candidates:
                buckets.setdefault((f.tuple_key, f.as_of), []).append((rec, f, sid))

            for (tk, as_of), items in buckets.items():
                facts = [f for _, f, _ in items]
                # assign provisional ids by insertion below; conflict.detect uses value/bar only
                fids = []
                for rec, f, sid in items:
                    # dedup: same tuple/as_of/value/source already present?
                    cur = await self._conn.execute(
                        "SELECT id FROM fact WHERE tuple_key=? AND IFNULL(as_of,-1)=IFNULL(?,-1) "
                        "AND value=? AND IFNULL(unit,'')=IFNULL(?,'') AND source_id=?",
                        (tk, as_of, f.value, f.unit, sid),
                    )
                    if await cur.fetchone():
                        continue
                    c = await self._conn.execute(
                        "INSERT INTO fact (tuple_key, qualifiers_json, as_of, value, unit, source_id, "
                        "admission, lifecycle, run_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (tk, json.dumps({}), as_of, f.value, f.unit, sid, "provisional", "current", run_id, now),
                    )
                    f.fact_id = c.lastrowid
                    fids.append(f.fact_id)
                    await self._conn.execute(
                        "INSERT INTO evidence (fact_id, quoted_span, retrieved_at) VALUES (?,?,?)",
                        (f.fact_id, rec.get("evidence_span"), now),
                    )
                    await self._conn.execute(
                        "INSERT INTO fact_revision (fact_id, change, cause, why, created_at) VALUES (?,?,?,?,?)",
                        (f.fact_id, f"value={f.value}", "ingest", "new fact from run", now),
                    )
                # conflict detection over the bucket's freshly-inserted facts
                bucket_facts = [f for _, f, _ in items if f.fact_id is not None]
                for intent in conflict.detect(bucket_facts):
                    if isinstance(intent, model.OpenConflict):
                        cc = await self._conn.execute(
                            "INSERT INTO conflict (tuple_key, as_of, status, created_at) VALUES (?,?, 'open', ?)",
                            (tk, as_of, now),
                        )
                        for fid in intent.fact_ids:
                            await self._conn.execute(
                                "INSERT INTO conflict_member (conflict_id, fact_id) VALUES (?,?)",
                                (cc.lastrowid, fid),
                            )
                has_open = any(isinstance(i, model.OpenConflict) for i in conflict.detect(bucket_facts))
                for f in bucket_facts:
                    if promotion.evaluate(f, bucket_facts, has_open_conflict=has_open) and not has_open \
                       and f.source_meets_bar and not f.has_unspecified_required:
                        await self._conn.execute(
                            "UPDATE fact SET admission='trusted' WHERE id=?", (f.fact_id,))
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    async def _source_id(self, url: str, now: str) -> int:
        cur = await self._conn.execute("SELECT id FROM source WHERE url_or_domain=?", (url,))
        row = await cur.fetchone()
        if row:
            return row[0]
        tier = self._registry.tier(url)
        c = await self._conn.execute(
            "INSERT INTO source (url_or_domain, tier, flags_json) VALUES (?,?,?)",
            (url, tier, json.dumps(self._registry.flags(url))),
        )
        return c.lastrowid
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_factbase_ingest.py -v`
Expected: PASS (all 3).

- [ ] **Step 5: Run full suite + commit**

```bash
uv run pytest -q -p no:warnings
git add src/open_deep_research/factbase/ingest.py tests/test_factbase_ingest.py
git commit -m "feat(factbase): Ingestor (resolve/identity/registry -> conflict/promotion -> atomic write)"
```

---

### Task 7: run lifecycle — preallocate + finalize ports in storage

**Files:**
- Modify: `src/open_deep_research/storage.py`
- Test: `tests/test_run_lifecycle.py`

**Context:** Add `preallocate_run(db_path, thread_id) -> run_id` (INSERT a `research_runs` row with `status='running'`, the thread_id, `last_heartbeat=now`) and `finalize_research_run(db_path, run_id, fields)` (UPDATE the row to `completed`/`error` with topic/report/etc.). These are additive — existing `save_run_and_upsert_subject` stays for now; Task 8 calls finalize. (Full INSERT→UPDATE refactor of subject upsert is deferred to keep this task small; the preallocated row is finalized in place.)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_run_lifecycle.py
import asyncio
from open_deep_research import storage
from open_deep_research.factbase import migrations, schema
import aiosqlite

def test_preallocate_then_finalize_updates_same_row(tmp_path):
    db = str(tmp_path / "t.db")
    async def run():
        # ensure base research_runs + factbase columns exist
        async with aiosqlite.connect(db) as conn:
            await storage._ensure_schema(conn)
            await migrations.apply(conn, schema.STEPS)
        rid = await storage.preallocate_run(db, "thread-7")
        assert isinstance(rid, int)
        await storage.finalize_research_run(db, rid, {"status": "completed", "topic": "X"})
        async with aiosqlite.connect(db) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT status, topic, thread_id FROM research_runs WHERE id=?", (rid,))
            row = await cur.fetchone()
            assert row["status"] == "completed" and row["topic"] == "X" and row["thread_id"] == "thread-7"
            cur = await conn.execute("SELECT COUNT(*) FROM research_runs")
            assert (await cur.fetchone())[0] == 1   # finalize UPDATED, did not insert a 2nd row
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_run_lifecycle.py -v`
Expected: FAIL (`preallocate_run` not defined).

- [ ] **Step 3: Implement (append to storage.py)**

```python
async def preallocate_run(db_path: str, thread_id: str) -> int:
    """Insert a research_runs row with status='running' and return its id."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(db_path) as conn:
        await _ensure_schema(conn)
        from open_deep_research.factbase import migrations, schema
        await migrations.apply(conn, schema.STEPS)  # ensure lifecycle columns exist
        cur = await conn.execute(
            "INSERT INTO research_runs (thread_id, status, last_heartbeat, created_at) VALUES (?,?,?,?)",
            (thread_id, "running", now, now),
        )
        await conn.commit()
        return cur.lastrowid


async def finalize_research_run(db_path: str, run_id: int, fields: dict) -> None:
    """UPDATE the preallocated row to its terminal state. Idempotent on status."""
    allowed = {"status", "topic", "research_brief", "final_report", "sources",
               "raw_notes", "config", "error", "coverage_incomplete"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    cols = ", ".join(f"{k}=?" for k in sets)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(f"UPDATE research_runs SET {cols} WHERE id=?", (*sets.values(), run_id))
        await conn.commit()
```

(Note: `research_runs.thread_id` and `status`/`last_heartbeat` come from the Task 1 migration; `sources`/`raw_notes`/`config` already exist in the legacy schema as TEXT — pass JSON strings.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_run_lifecycle.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/storage.py tests/test_run_lifecycle.py
git commit -m "feat(factbase): preallocate_run + finalize_research_run lifecycle ports"
```

---

### Task 8: Wire extract_facts + preallocate into the graph

**Files:**
- Modify: `src/open_deep_research/deep_researcher.py` (add `extract_facts` node + `preallocate_run` node + edges)
- Modify: `src/open_deep_research/state.py` (no new field needed — `extract_facts` reads run_source by thread_id and writes to DB directly)
- Test: `tests/test_graph_extract_facts_wiring.py`

**Context:** Insert `extract_facts` between `final_report_generation` and `persist_research` (research path only). `extract_facts` reads run_source by `thread_id`, runs `FactExtractor` per `raw_text` source using `configurable_claude_model(...).with_structured_output(...)` for the real model call, then `Ingestor.ingest`. Preallocate at START. Keep it best-effort: failure logs, never breaks the run. The model wiring is the one piece not unit-tested here (it needs a live backend); the **wiring test** verifies the node + edges exist and that `extract_facts` is a no-op safe path when `persist_results` is off.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_graph_extract_facts_wiring.py
from open_deep_research import deep_researcher as dr

def test_graph_has_extract_facts_node_on_research_path():
    g = dr.deep_researcher  # compiled graph
    nodes = set(g.get_graph().nodes.keys())
    assert "extract_facts" in nodes
    assert "preallocate_run" in nodes

def test_extract_facts_noop_when_persist_disabled():
    import asyncio
    from langchain_core.runnables import RunnableConfig
    # persist_results off -> extract_facts returns without touching the DB
    state = {"messages": [], "research_brief": "x"}
    cfg = RunnableConfig(configurable={"persist_results": False, "thread_id": "t-noop"})
    out = asyncio.run(dr.extract_facts(state, cfg))
    assert out == {} or out is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_graph_extract_facts_wiring.py -v`
Expected: FAIL (`extract_facts`/`preallocate_run` nodes absent).

- [ ] **Step 3: Implement**

Add the nodes in deep_researcher.py (near the other node functions):

```python
async def preallocate_run(state: AgentState, config: RunnableConfig) -> dict:
    """Create the research_runs row early so the tool layer/extract_facts have a run_id."""
    configurable = Configuration.from_runnable_config(config)
    if not configurable.persist_results:
        return {}
    thread_id = (config.get("configurable") or {}).get("thread_id")
    try:
        run_id = await preallocate_run_storage(get_db_path(config), str(thread_id))
        return {"prealloc_run_id": run_id}
    except Exception as e:
        logger.warning("preallocate_run failed (non-fatal): %s", e)
        return {}


async def extract_facts(state: AgentState, config: RunnableConfig) -> dict:
    """Per-source fact extraction over the run's captured run_source rows (research path)."""
    configurable = Configuration.from_runnable_config(config)
    if not configurable.persist_results:
        return {}
    thread_id = (config.get("configurable") or {}).get("thread_id")
    if not thread_id:
        return {}
    try:
        import aiosqlite
        from open_deep_research.factbase import (store, extractor, ingest, profile as fbprofile,
                                                 entities, registry)
        prof = fbprofile.load("country_digital_identity")
        reg = registry.SourceRegistry.load("di_source_registry")
        # Build a model_call that asks the configured model for FactRecords (structured output).
        model_call = _make_fact_model_call(configurable, config, prof)
        run_id = state.get("prealloc_run_id")
        async with aiosqlite.connect(get_db_path(config)) as conn:
            from open_deep_research.factbase import migrations as fbmig, schema as fbschema
            await fbmig.apply(conn, fbschema.STEPS)
            sources = await store.RunSourceStore(conn).read(str(thread_id))
            all_records = []
            for s in sources:
                if s["capture_status"] != "raw_text" or not s["text"]:
                    continue
                recs = await extractor.extract(s["text"], prof, model_call)
                for r in recs:
                    r.setdefault("source_url", s["source_url"])
                all_records.extend(recs)
            if all_records and run_id:
                await ingest.Ingestor(conn, profile=prof, resolver=entities.CountryResolver(),
                                      registry=reg).ingest(run_id=run_id, records=all_records)
    except Exception as e:
        logger.warning("extract_facts failed (non-fatal): %s", e)
    return {}
```

Add a `_make_fact_model_call(configurable, config, prof)` helper that returns an async `model_call(source_text, prof)` using `configurable_claude_model(...)` with structured output of a `list[FactRecord]` pydantic model (define `FactRecord`/`ExtractionResult` pydantic models near the other structured-output models, with fields: property, instance_name, value, unit, as_of, qualifiers (dict[str,str|None]), evidence_span). On any backend error return `[]`.

Add `prealloc_run_id: int | None` to `AgentState` in state.py (so the node return survives to extract_facts).

Wire nodes/edges (near deep_researcher.py:1030-1045):
```python
deep_researcher_builder.add_node("preallocate_run", preallocate_run)
deep_researcher_builder.add_node("extract_facts", extract_facts)
# START -> preallocate_run -> assess_knowledge (replace the START->assess_knowledge edge)
deep_researcher_builder.add_edge(START, "preallocate_run")
deep_researcher_builder.add_edge("preallocate_run", "assess_knowledge")
# research path: final_report_generation -> extract_facts -> persist_research
deep_researcher_builder.add_edge("final_report_generation", "extract_facts")
deep_researcher_builder.add_edge("extract_facts", "persist_research")
```
Remove/replace the old `add_edge(START, "assess_knowledge")` and `add_edge("final_report_generation", "persist_research")`. Import `preallocate_run as preallocate_run_storage` and `get_db_path` from storage.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_graph_extract_facts_wiring.py -v`
Expected: PASS.

- [ ] **Step 5: Run full suite + commit**

```bash
uv run pytest -q -p no:warnings
git add src/open_deep_research/deep_researcher.py src/open_deep_research/state.py tests/test_graph_extract_facts_wiring.py
git commit -m "feat(factbase): wire preallocate_run + extract_facts into the graph"
```

---

### Task 9: End-to-end smoke (stubbed model) — a run produces facts

**Files:**
- Test: `tests/test_factbase_e2e_ingest.py`

**Context:** Prove the full extraction→ingestion path with a stub model and hand-written run_source rows (no live LLM, no full graph run): record two conflicting run_source-derived records and assert facts + conflict land. This locks the contract the graph node depends on.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_e2e_ingest.py
import asyncio, aiosqlite
from open_deep_research.factbase import migrations, schema, store, extractor, ingest, profile, entities, registry

DI = profile.load("country_digital_identity")

def test_run_sources_to_facts_end_to_end():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            rs = store.RunSourceStore(conn)
            await rs.record("t1", "https://id4d.worldbank.org/x", "India coverage 99% adults", capture_status="raw_text")
            await rs.record("t1", "https://gsma.com/y", "India coverage 87% adults", capture_status="raw_text")

            def stub_for(url):
                val = "99" if "id4d" in url else "87"
                async def _call(text, prof):
                    return [{"property":"id_coverage_pct","instance_name":"India","value":val,"unit":"%",
                             "as_of":"2024","qualifiers":{"population_basis":"adults_15plus"},
                             "evidence_span": f"coverage {val}%"}]
                return _call

            all_records = []
            for s in await rs.read("t1"):
                # make the span verifiable against the stored text
                recs = await extractor.extract(s["text"].replace("coverage ", "coverage ") + " coverage 99% coverage 87%",
                                               DI, stub_for(s["source_url"]))
                for r in recs:
                    r["source_url"] = s["source_url"]
                all_records += recs

            await ingest.Ingestor(conn, profile=DI, resolver=entities.CountryResolver(),
                                  registry=registry.SourceRegistry.load("di_source_registry")).ingest(
                run_id=1, records=all_records)

            cur = await conn.execute("SELECT COUNT(*) FROM fact"); assert (await cur.fetchone())[0] == 2
            cur = await conn.execute("SELECT COUNT(*) FROM conflict WHERE status='open'"); assert (await cur.fetchone())[0] == 1
    asyncio.run(run())
```

(If the span-normalization makes both spans verifiable in each source, adjust the stub source text so each record's `evidence_span` is a substring of its own source text — the point is two trust-bar values in one tuple/as_of → one conflict, two facts.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_factbase_e2e_ingest.py -v`
Expected: FAIL until Tasks 5–6 exist (they will, by now) — if it fails on span assertions, fix the stub text so spans verify.

- [ ] **Step 3: (no new code — adjust the test's stub source text only so spans verify)**

- [ ] **Step 4: Run + full suite**

Run: `uv run pytest -q -p no:warnings`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tests/test_factbase_e2e_ingest.py
git commit -m "test(factbase): end-to-end run_source -> extract -> ingest (stub model)"
```

---

## Plan 2 complete

After Task 9: a real Tavily-backed research run captures per-source text (`run_source`), the `extract_facts` node extracts qualifier-aware facts per source with span verification, and the `Ingestor` writes gated, conflict-aware facts with provenance — wired into the live graph. The fact base is now **populated by actual runs** (for the Tavily backend; summarizing backends record `summarized` rows and skip extraction).

**Open follow-ups → Plan 3 / Plan 4:**
- `dossier show <country>` / `compare <property>` + CSV/MD export (Plan 3) — *the read surface to see what this produces.*
- Instrumentation (coverage, groundedness, false-conflict + drop-rate), the registry-version recompute pass, the stale-`running` reaper, and the `coverage_incomplete` derivation from skipped sources (Plan 4).
- Reconcile the 3 minor follow-ups from Plan 1's final review (AutoClose per-group as_of; `trust_threshold` REAL↔str; dataclass↔pydantic).
- Real `_make_fact_model_call` prompt calibration ("explicitly states; else unspecified") — the false-conflict/drop-rate dial.
