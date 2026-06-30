# Cross-Run Source Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deduplicate source content across runs into a `source_content` table that doubles as a summary cache, so the dominant per-source LLM summarization is reused (not re-paid) and duplicate search hits disappear.

**Architecture:** A new `source_content` table holds one row per unique `content_hash` (the raw text + its summary). `run_source` stays as the per-run capture/provenance row and joins to `source_content` by the `content_hash` it already carries. Delivered in two phases: **A** is purely additive (dual-write + summary cache, no regression to ①); **B** cuts over reads, the FTS index, and storage onto `source_content`.

**Tech Stack:** Python 3, `aiosqlite`, SQLite FTS5, the existing versioned migration framework (`factbase/migrations.py`). Tests: `asyncio.run(...)` over `:memory:` DBs + `monkeypatch` for model stubs (house style).

## Global Constraints

- **Cache identity = `(content_hash, summary_model, SUMMARY_PROMPT_VERSION)`.** A summary is reused only if all three match. `SUMMARY_PROMPT_VERSION` is a constant bumped only on *intentional* `summarize_webpage_prompt` edits. The current date is **excluded** (and in fact the prompt template has no `{date}` placeholder, so `get_today_str()` is already dropped by `.format()`).
- **`source_content` rows only for non-empty raw text** (`capture_status='raw_text' AND text IS NOT NULL AND text <> ''`) — at the capture-path upsert **and** the Phase B backfill — so empty captures (all sharing `sha256('')`) never collapse into one junk row.
- **Phasing (refines spec §Migration for coherence):** Phase A is **additive dual-write** (keeps writing `run_source.text`, leaves ①'s `fts_source`/read/extraction untouched) so it ships zero-regression. The **read-path migration, FTS re-point, `run_source.text` null, and stop-dual-write all land together in Phase B** — so the extractor is never starved (codex round-1 High: read-path in place *before* any null).
- **Best-effort:** a summary-cache miss/store failure, a dedup-insert race, or a backfill error never fails a run (mirror today's `record`/`_finalize_search` try/except posture). `INSERT OR IGNORE` + `content_hash UNIQUE` make dedup race-safe.
- **Migrations:** latest is **v12**. Phase A adds **v13** (create `source_content`). Phase B adds **v14** (drop stale fts + backfill + null). Re-confirm numbers against `schema.STEPS` at implementation time. Trigger/FTS DDL goes via `search_schema.ensure_search_schema` (executescript), never STEPS (the `;`-splitter corrupts trigger bodies).
- **Out of scope:** reducing search-API calls; time TTL; the prompt-sentence `subjects.name` fix (Polish); ③/④.

---

## PHASE A — additive: `source_content` + dual-write + summary cache

### Task A1: Migration v13 — create `source_content`

**Files:**
- Modify: `src/open_deep_research/factbase/schema.py` (append a tuple to `STEPS`, after the `(12, …)` entry)
- Test: `tests/test_factbase_source_content_schema.py`

**Interfaces:**
- Produces: a `source_content` table with columns `id, content_hash UNIQUE, source_url, title, text, summary, summary_model, summary_prompt_version, first_seen_at, soft_deleted_at`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_source_content_schema.py
import asyncio, aiosqlite
from open_deep_research.factbase import schema, migrations


def test_v13_creates_source_content():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, thread_id TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            cur = await conn.execute("PRAGMA table_info(source_content)")
            cols = {r[1] for r in await cur.fetchall()}
            assert {"content_hash", "text", "summary", "summary_model",
                    "summary_prompt_version"} <= cols
            # content_hash is UNIQUE
            await conn.execute("INSERT INTO source_content (content_hash, text) VALUES ('h','t')")
            try:
                await conn.execute("INSERT INTO source_content (content_hash, text) VALUES ('h','t2')")
                raised = False
            except aiosqlite.IntegrityError:
                raised = True
            assert raised
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src /mnt/c/Users/abradley/Projects/IdentityInnovation/search/open_deep_research/.venv/bin/python -m pytest tests/test_factbase_source_content_schema.py -v`
Expected: FAIL — `no such table: source_content`.

- [ ] **Step 3: Append the v13 step**

In `src/open_deep_research/factbase/schema.py`, add after the `(12, …)` tuple, before the closing `]`:

```python
    (13, """
    CREATE TABLE IF NOT EXISTS source_content (
        id INTEGER PRIMARY KEY,
        content_hash TEXT UNIQUE,
        source_url TEXT,
        title TEXT,
        text TEXT,
        summary TEXT,
        summary_model TEXT,
        summary_prompt_version TEXT,
        first_seen_at TEXT,
        soft_deleted_at TEXT
    );
    """),
```

- [ ] **Step 4: Run test to verify it passes**

Run the command from Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_factbase_source_content_schema.py src/open_deep_research/factbase/schema.py
git commit -m "feat(factbase): source_content table (schema v13)"
```

---

### Task A2: Capture path dual-writes `source_content`

**Files:**
- Modify: `src/open_deep_research/factbase/store.py` (`RunSourceStore.record`)
- Test: `tests/test_source_content_capture.py`

**Interfaces:**
- Consumes: `source_content` (A1).
- Produces: `record(...)` upserts one `source_content` row per unique non-empty `content_hash` (idempotent via `INSERT OR IGNORE`), **in addition to** the existing `run_source` insert (unchanged). No signature change.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_source_content_capture.py
import asyncio, aiosqlite
from open_deep_research.factbase import schema, migrations, store


async def _db():
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, thread_id TEXT);")
    await conn.commit()
    await migrations.apply(conn, schema.STEPS)
    return conn


def test_dedup_one_content_two_captures():
    async def run():
        conn = await _db()
        rs = store.RunSourceStore(conn)
        await rs.record("tA", "https://x/a", "same body", capture_status="raw_text", title="T")
        await rs.record("tB", "https://x/a", "same body", capture_status="raw_text", title="T")
        sc = await (await conn.execute("SELECT count(*) FROM source_content")).fetchone()
        cap = await (await conn.execute("SELECT count(*) FROM run_source")).fetchone()
        assert sc[0] == 1 and cap[0] == 2
        row = await (await conn.execute(
            "SELECT text, title FROM source_content")).fetchone()
        assert row[0] == "same body" and row[1] == "T"
        await conn.close()
    asyncio.run(run())


def test_no_source_content_for_empty_capture():
    async def run():
        conn = await _db()
        rs = store.RunSourceStore(conn)
        await rs.record("tA", "https://x/none", None, capture_status="summarized")
        sc = await (await conn.execute("SELECT count(*) FROM source_content")).fetchone()
        assert sc[0] == 0
        await conn.close()
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/test_source_content_capture.py -v`
Expected: FAIL — `source_content` empty (no upsert yet).

- [ ] **Step 3: Add the dual-write to `record()`**

In `src/open_deep_research/factbase/store.py`, replace `record` with (the new block is the `if text` upsert at the top; the rest is unchanged):

```python
    async def record(self, thread_id: str, url: str, text: str | None, *,
                     capture_status: str, reason: str | None = None,
                     title: str | None = None) -> None:
        ch = _hash(text)
        # Dedup unique content into source_content (raw, non-empty only) so the
        # text + its summary are stored once across runs. Idempotent.
        if text:
            await self._conn.execute(
                "INSERT OR IGNORE INTO source_content "
                "(content_hash, source_url, title, text, first_seen_at) VALUES (?,?,?,?,?)",
                (ch, url, title, text, datetime.now(timezone.utc).isoformat()))
        cur = await self._conn.execute(
            "SELECT 1 FROM run_source WHERE thread_id=? AND source_url=? AND content_hash=?",
            (thread_id, url, ch))
        if await cur.fetchone():
            await self._conn.commit()
            return
        await self._conn.execute(
            "INSERT INTO run_source (thread_id, source_url, capture_status, reason, text, title, content_hash, retrieved_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (thread_id, url, capture_status, reason, text, title, ch,
             datetime.now(timezone.utc).isoformat()))
        await self._conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run the Step 2 command. Expected: PASS (both tests).

- [ ] **Step 5: Run the existing capture/title tests (no regression)**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/test_run_source_title_capture.py tests/test_factbase_backfill.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_source_content_capture.py src/open_deep_research/factbase/store.py
git commit -m "feat(factbase): dual-write unique content into source_content"
```

---

### Task A3: Cross-run summary cache in `_finalize_search`

**Files:**
- Modify: `src/open_deep_research/utils.py` (`_finalize_search`; add a `SUMMARY_PROMPT_VERSION` constant)
- Test: `tests/test_summary_cache.py`

**Interfaces:**
- Consumes: `source_content` (A1), populated by `record_search_sources` → `record` (A2).
- Produces: before summarizing, reuse `source_content.summary` when `(content_hash, summary_model, SUMMARY_PROMPT_VERSION)` matches; after summarizing, persist it. `SUMMARY_PROMPT_VERSION: str` module constant in `utils.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_summary_cache.py
import asyncio, aiosqlite
import open_deep_research.utils as utils
from open_deep_research.factbase import schema, migrations, store


def _stub_config(model="claude:haiku"):
    return type("C", (), {"summarize_search_results": True, "max_content_length": 5000,
        "summarization_model": model, "summarization_model_max_tokens": 1000,
        "max_structured_output_retries": 3, "persist_results": False,
        "model_chain": lambda *a, **k: [model]})()


def test_summary_reused_across_runs_skips_model(monkeypatch):
    async def run():
        conn = await aiosqlite.connect(":memory:")
        await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, thread_id TEXT);")
        await conn.commit()
        await migrations.apply(conn, schema.STEPS)
        rs = store.RunSourceStore(conn)
        # content already captured + summarized by a prior run:
        await rs.record("t0", "http://e", "FULL TEXT", capture_status="raw_text")
        from open_deep_research.factbase.store import _hash
        await conn.execute(
            "UPDATE source_content SET summary=?, summary_model=?, summary_prompt_version=? WHERE content_hash=?",
            ("CACHED", "claude:haiku", utils.SUMMARY_PROMPT_VERSION, _hash("FULL TEXT")))
        await conn.commit()

        calls = {"n": 0}
        async def _fake_summarize(model, text):
            calls["n"] += 1
            return "FRESH"
        monkeypatch.setattr(utils, "summarize_webpage", _fake_summarize)
        # route the cache to OUR conn (the resolver passes a conn/db_path — see Step 3)
        summary = await utils._lookup_cached_summary(conn, _hash("FULL TEXT"), "claude:haiku")
        assert summary == "CACHED" and calls["n"] == 0
        # a different model is NOT reused:
        assert await utils._lookup_cached_summary(conn, _hash("FULL TEXT"), "claude:opus") is None
        await conn.close()
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/test_summary_cache.py -v`
Expected: FAIL — `utils` has no `SUMMARY_PROMPT_VERSION` / `_lookup_cached_summary`.

- [ ] **Step 3: Implement the cache helpers + wire into `_finalize_search`**

In `src/open_deep_research/utils.py`, add near the top (after `_SUMMARY_CACHE`):

```python
# Bump only when summarize_webpage_prompt is intentionally changed. Part of the
# cross-run summary cache identity (content_hash, summary_model, this). The date
# the prompt formats in is NOT a placeholder in the template, so it never varies
# the output and is correctly excluded.
SUMMARY_PROMPT_VERSION = "v1"


async def _lookup_cached_summary(conn, content_hash: str, model: str) -> str | None:
    """Return a cross-run summary for this exact content+model+prompt, else None."""
    cur = await conn.execute(
        "SELECT summary FROM source_content "
        "WHERE content_hash=? AND summary IS NOT NULL AND summary_model=? AND summary_prompt_version=?",
        (content_hash, model, SUMMARY_PROMPT_VERSION))
    row = await cur.fetchone()
    return row[0] if row else None


async def _store_cached_summary(conn, content_hash: str, summary: str, model: str) -> None:
    await conn.execute(
        "UPDATE source_content SET summary=?, summary_model=?, summary_prompt_version=? WHERE content_hash=?",
        (summary, model, SUMMARY_PROMPT_VERSION, content_hash))
    await conn.commit()
```

Then, in `_finalize_search`, thread a DB connection through summarization. The function already opens `aiosqlite.connect(...)` and runs `record_search_sources` inside a `with` block; **keep a second short-lived connection for the summary cache** so the model-call loop can read/write `source_content`. Replace the `_summarize_one` body's model path:

```python
    # resolve the db path once (None when persistence is off → cache disabled)
    from open_deep_research.storage import get_db_path as _get_db_path
    _model_id = configurable.summarization_model
    _db = _get_db_path(config) if (configurable.persist_results and thread_id) else None

    async def _summarize_one(result):
        raw = result.get("raw_content")
        url = result.get("url")
        if not do_summarize:
            return result.get("content") or (raw or "")[:max_char_to_include] or None
        if url and url in cache:
            return cache[url]
        if not raw:
            return None
        ch = _fb_hash(raw)
        if _db:                                   # L2: cross-run cache
            try:
                async with _aiosqlite.connect(_db) as c2:
                    hit = await _lookup_cached_summary(c2, ch, _model_id)
                if hit is not None:
                    if url: cache[url] = hit
                    return hit
            except Exception as _e:
                logger.warning("summary-cache read failed (non-fatal): %s", _e)
        async with _summarize_semaphore():
            summary = await summarize_webpage(summarization_model, raw[:max_char_to_include])
        if _db:
            try:
                async with _aiosqlite.connect(_db) as c2:
                    await _store_cached_summary(c2, ch, summary, _model_id)
            except Exception as _e:
                logger.warning("summary-cache write failed (non-fatal): %s", _e)
        if url:
            cache[url] = summary
        return summary
```

Add the imports used above near the top of `utils.py` if not present: `import aiosqlite as _aiosqlite` and `from open_deep_research.factbase.store import _hash as _fb_hash`.

> Note: `_store_cached_summary` UPDATEs an existing `source_content` row (created by `record_search_sources` earlier in `_finalize_search`). If persistence is off, `_db` is None and the cache is simply skipped (no behavior change).

- [ ] **Step 4: Run test to verify it passes**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 5: Run the search-backend tests (no regression)**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/test_search_backends.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_summary_cache.py src/open_deep_research/utils.py
git commit -m "feat(search): cross-run summary cache keyed by (content_hash, model, prompt_version)"
```

---

## PHASE B — cutover: read-path, FTS re-point, backfill, storage reclaim

### Task B1: `RunSourceStore.read()` sources text from `source_content`

**Files:**
- Modify: `src/open_deep_research/factbase/store.py` (`read`)
- Test: `tests/test_read_path_coalesce.py`

**Interfaces:**
- Produces: `read(thread_id)` returns each capture's `text` via `COALESCE(run_source.text, source_content.text)` joined by `content_hash`, plus `title`. Consumers (`extraction.py`) are unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_read_path_coalesce.py
import asyncio, aiosqlite
from open_deep_research.factbase import schema, migrations, store


def test_read_returns_text_from_source_content_when_run_source_null():
    async def run():
        conn = await aiosqlite.connect(":memory:")
        await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, thread_id TEXT);")
        await conn.commit()
        await migrations.apply(conn, schema.STEPS)
        rs = store.RunSourceStore(conn)
        await rs.record("t1", "http://a", "BODY", capture_status="raw_text", title="T")
        # simulate Phase B null of run_source.text (text lives in source_content)
        await conn.execute("UPDATE run_source SET text=NULL")
        await conn.commit()
        rows = await rs.read("t1")
        assert rows[0]["text"] == "BODY" and rows[0]["title"] == "T"
        await conn.close()
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/test_read_path_coalesce.py -v`
Expected: FAIL — `read()` returns `text=None` (selects `run_source.text` only).

- [ ] **Step 3: Update `read()`**

In `src/open_deep_research/factbase/store.py`, replace `read`:

```python
    async def read(self, thread_id: str) -> list[dict]:
        self._conn.row_factory = aiosqlite.Row
        cur = await self._conn.execute(
            "SELECT rs.id, rs.source_url, rs.capture_status, rs.reason, rs.title, "
            "       COALESCE(rs.text, sc.text) AS text "
            "FROM run_source rs "
            "LEFT JOIN source_content sc ON sc.content_hash = rs.content_hash "
            "WHERE rs.thread_id=? AND rs.soft_deleted_at IS NULL",
            (thread_id,))
        return [dict(r) for r in await cur.fetchall()]
```

- [ ] **Step 4: Run test + extraction regression**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/test_read_path_coalesce.py -q && PYTHONPATH=src …/.venv/bin/python -m pytest tests/ -k "extraction or backfill" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_read_path_coalesce.py src/open_deep_research/factbase/store.py
git commit -m "feat(factbase): read() sources text via COALESCE(run_source, source_content)"
```

---

### Task B2: Re-point `fts_source` to `source_content` (index + search join)

**Files:**
- Modify: `src/open_deep_research/factbase/search_schema.py` (`_SEARCH_SCHEMA`, `ensure_search_schema` self-heal, the backfill content-table map)
- Modify: `src/open_deep_research/factbase/search.py` (`_source_hits`)
- Test: `tests/test_fts_source_content.py`

**Interfaces:**
- Consumes: `source_content` (A1/A2), `read`-path (B1).
- Produces: `fts_source` indexes `source_content`; `ensure_search_schema` drops a stale `run_source`-based `fts_source` and recreates it over `source_content`; `_source_hits` returns one hit per content, subject resolved via any capture.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fts_source_content.py
import asyncio, aiosqlite
from open_deep_research.factbase import schema, migrations, search_schema, search


async def _seed(conn):
    await conn.executescript("""
        CREATE TABLE subjects (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT, name TEXT);
        CREATE TABLE research_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id INTEGER, thread_id TEXT);
    """)
    await conn.commit()
    await migrations.apply(conn, schema.STEPS)
    await search_schema.ensure_search_schema(conn)
    await conn.execute("INSERT INTO subjects (id, slug, name) VALUES (1,'estonia','Estonia')")
    await conn.execute("INSERT INTO research_runs (id, subject_id, thread_id) VALUES (1,1,'t-est')")
    # same content captured by two runs (would be 2 hits pre-dedup) -> one source_content row
    await conn.execute("INSERT INTO source_content (id, content_hash, source_url, title, text) "
                       "VALUES (1,'h1','http://roca','ROCA','The ROCA vulnerability in Estonian id-kaart')")
    await conn.execute("INSERT INTO run_source (thread_id, source_url, capture_status, content_hash) "
                       "VALUES ('t-est','http://roca','raw_text','h1')")
    await conn.execute("INSERT INTO run_source (thread_id, source_url, capture_status, content_hash) "
                       "VALUES ('t-other','http://roca','raw_text','h1')")
    await conn.commit()


def test_one_hit_per_content_and_subject_via_capture():
    async def run():
        conn = await aiosqlite.connect(":memory:")
        await _seed(conn)
        hits = await search.search_research(conn, "ROCA", kinds=("source",))
        assert len(hits) == 1 and hits[0].ref_id == 1            # deduped
        assert hits[0].subject == "EST"                          # via the t-est capture
        assert (await search.search_research(conn, "ROCA", subject="Estonia", kinds=("source",)))
        assert await search.search_research(conn, "ROCA", subject="Germany", kinds=("source",)) == []
        await conn.close()
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/test_fts_source_content.py -v`
Expected: FAIL — `fts_source` still indexes `run_source` (2 hits, or wrong rowids).

- [ ] **Step 3: Re-point the FTS schema + self-heal**

In `src/open_deep_research/factbase/search_schema.py`: change the `fts_source` virtual table and its triggers to target `source_content`, and make `ensure_search_schema` drop a stale `run_source`-based `fts_source`. Replace the `fts_source` block of `_SEARCH_SCHEMA`:

```python
CREATE VIRTUAL TABLE IF NOT EXISTS fts_source USING fts5(
    text, source_url, title,
    content='source_content', content_rowid='id'
);
```
and the three `run_source_*` triggers with `source_content_*` triggers:
```python
CREATE TRIGGER IF NOT EXISTS source_content_ai AFTER INSERT ON source_content BEGIN
    INSERT INTO fts_source(rowid, text, source_url, title)
        VALUES (new.id, new.text, new.source_url, new.title);
END;
CREATE TRIGGER IF NOT EXISTS source_content_ad AFTER DELETE ON source_content BEGIN
    INSERT INTO fts_source(fts_source, rowid, text, source_url, title)
        VALUES ('delete', old.id, old.text, old.source_url, old.title);
END;
CREATE TRIGGER IF NOT EXISTS source_content_au AFTER UPDATE ON source_content BEGIN
    INSERT INTO fts_source(fts_source, rowid, text, source_url, title)
        VALUES ('delete', old.id, old.text, old.source_url, old.title);
    INSERT INTO fts_source(rowid, text, source_url, title)
        VALUES (new.id, new.text, new.source_url, new.title);
END;
```
Update the backfill content-table map in `ensure_search_schema`:
```python
    for fts, content in (("fts_source", "source_content"), ("fts_fact", "fact")):
```
Add a self-heal at the very top of `ensure_search_schema` (before `executescript`), so an old run_source-based `fts_source` is dropped and recreated over `source_content`:
```python
    cur = await conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='fts_source' AND type='table'")
    row = await cur.fetchone()
    if row and "content='run_source'" in (row[0] or ""):
        await conn.executescript(
            "DROP TRIGGER IF EXISTS run_source_ai; DROP TRIGGER IF EXISTS run_source_ad;"
            " DROP TRIGGER IF EXISTS run_source_au; DROP TABLE IF EXISTS fts_source;")
```

- [ ] **Step 4: Re-point the search join**

In `src/open_deep_research/factbase/search.py`, replace `_source_hits` so `fts_source` joins `source_content`, and subject resolves via the capture join (one hit per content; matches any capturing subject):

```python
async def _source_hits(conn, match, target, limit):
    sql = f"""
        SELECT sc.id, sc.source_url, sc.title,
               bm25(fts_source) AS score, {_SNIPPET.format(tbl='fts_source')} AS snip
        FROM fts_source
        JOIN source_content sc ON sc.id = fts_source.rowid
        WHERE fts_source MATCH ? AND sc.soft_deleted_at IS NULL
        ORDER BY score LIMIT ?
    """
    conn.row_factory = aiosqlite.Row
    cur = await conn.execute(sql, (match, limit))
    rows = await cur.fetchall()
    out = []
    for row in rows:
        # subjects that captured this content (via run_source -> research_runs -> subjects)
        subj_cur = await conn.execute(
            "SELECT DISTINCT s.name FROM run_source rs "
            "JOIN research_runs r ON r.thread_id = rs.thread_id "
            "JOIN subjects s ON s.id = r.subject_id "
            "WHERE rs.content_hash = (SELECT content_hash FROM source_content WHERE id=?)",
            (row["id"],))
        subjects = {CountryResolver().resolve(n[0]) for n in await subj_cur.fetchall() if n[0]}
        subjects.discard(None)
        if target is not None and target not in subjects:
            continue
        subj = target if target is not None else (sorted(subjects)[0] if subjects else None)
        out.append(Hit(kind="source", ref_id=row["id"], subject=subj,
                       snippet=row["snip"], score=-row["score"],
                       source_url=row["source_url"], title=row["title"], retrieved_at=None))
    return out
```

- [ ] **Step 5: Run test + ① search regression**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/test_fts_source_content.py tests/test_factbase_search.py tests/test_factbase_search_schema.py -q`
Expected: PASS. (If `test_factbase_search.py` seeds `run_source.text` for source hits, update its seed to insert a `source_content` row — note this in the commit.)

- [ ] **Step 6: Commit**

```bash
git add tests/test_fts_source_content.py src/open_deep_research/factbase/search_schema.py src/open_deep_research/factbase/search.py
git commit -m "feat(search): re-point fts_source to source_content (dedup hits, capture-join subjects)"
```

---

### Task B3: Backfill + null `run_source.text` + stop dual-write (migration v14)

**Files:**
- Modify: `src/open_deep_research/factbase/schema.py` (append v14)
- Modify: `src/open_deep_research/factbase/store.py` (`record` writes `run_source.text` as NULL for raw_text — text now lives only in `source_content`)
- Test: `tests/test_source_content_backfill.py`

**Interfaces:**
- Produces: existing raw-text rows deduped into `source_content`; `run_source.text` nulled; new raw-text captures store text only in `source_content`. Extraction/search unaffected (B1/B2).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_source_content_backfill.py
import asyncio, aiosqlite
from open_deep_research.factbase import schema, migrations


def test_backfill_dedups_and_excludes_empty_and_is_idempotent():
    async def run():
        conn = await aiosqlite.connect(":memory:")
        await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, thread_id TEXT);")
        await conn.commit()
        # apply through v13 only, seed legacy rows, then apply v14
        await migrations.apply(conn, [s for s in schema.STEPS if s[0] <= 13])
        await conn.execute("INSERT INTO run_source (thread_id, source_url, capture_status, text, content_hash) "
                           "VALUES ('t1','http://a','raw_text','BODY','hA')")
        await conn.execute("INSERT INTO run_source (thread_id, source_url, capture_status, text, content_hash) "
                           "VALUES ('t2','http://a','raw_text','BODY','hA')")  # dup content
        await conn.execute("INSERT INTO run_source (thread_id, source_url, capture_status, text, content_hash) "
                           "VALUES ('t3','http://b',  'summarized', NULL, ?)",
                           (__import__('hashlib').sha256(b'').hexdigest(),))      # empty capture
        await conn.commit()
        await migrations.apply(conn, schema.STEPS)          # runs v14
        sc = await (await conn.execute("SELECT count(*) FROM source_content")).fetchone()
        assert sc[0] == 1                                    # 'BODY' deduped; empty excluded
        txt = await (await conn.execute("SELECT text FROM source_content WHERE content_hash='hA'")).fetchone()
        assert txt[0] == "BODY"
        nulled = await (await conn.execute("SELECT count(*) FROM run_source WHERE text IS NOT NULL")).fetchone()
        assert nulled[0] == 0                                # run_source.text nulled
        await migrations.apply(conn, schema.STEPS)           # idempotent (v14 already applied)
        await conn.close()
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/test_source_content_backfill.py -v`
Expected: FAIL — no v14; `source_content` empty, `run_source.text` retained.

- [ ] **Step 3: Append the v14 backfill step**

In `src/open_deep_research/factbase/schema.py`, after the `(13, …)` tuple:

```python
    (14, """
    INSERT OR IGNORE INTO source_content (content_hash, source_url, title, text, first_seen_at)
        SELECT content_hash, MIN(source_url), MIN(title), MIN(text), MIN(retrieved_at)
        FROM run_source
        WHERE capture_status='raw_text' AND text IS NOT NULL AND text <> ''
        GROUP BY content_hash;
    UPDATE run_source SET text=NULL WHERE text IS NOT NULL;
    """),
```
(Both are single statements with no embedded `;`, so the STEPS `;`-splitter handles them.)

- [ ] **Step 4: Stop dual-writing `run_source.text`**

In `src/open_deep_research/factbase/store.py` `record()`, change the `run_source` INSERT to store `NULL` for text (the `source_content` upsert above already holds it):

```python
        await self._conn.execute(
            "INSERT INTO run_source (thread_id, source_url, capture_status, reason, text, title, content_hash, retrieved_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (thread_id, url, capture_status, reason, None, title, ch,
             datetime.now(timezone.utc).isoformat()))
```

- [ ] **Step 5: Run test + full regression**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/test_source_content_backfill.py tests/test_source_content_capture.py tests/test_read_path_coalesce.py tests/test_fts_source_content.py -q`
Expected: PASS.
Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/ -k "factbase or store or backfill or dossier or search or extraction or summary" -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_source_content_backfill.py src/open_deep_research/factbase/schema.py src/open_deep_research/factbase/store.py
git commit -m "feat(factbase): backfill source_content, null run_source.text, stop dual-write (v14)"
```

---

## Self-Review

**Spec coverage:** A1 = `source_content` table; A2 = capture dedup (non-empty guard); A3 = summary cache `(content_hash, summary_model, SUMMARY_PROMPT_VERSION)`; B1 = read-path COALESCE (codex High); B2 = FTS re-point + capture-join subjects (dedup hits, multi-subject); B3 = backfill (empty-filter, codex Medium-1) + null + stop dual-write. `evidence` untouched (no FK change). Phasing refinement documented in Global Constraints.

**Placeholder scan:** none — every step has full code/commands (the `…/.venv/bin/python` is the verified interpreter path, abbreviated in run lines for width; expand to `/mnt/c/Users/abradley/Projects/IdentityInnovation/search/open_deep_research/.venv/bin/python`).

**Type consistency:** `record(...)` signature unchanged across A2/B3; `_lookup_cached_summary(conn, content_hash, model)` / `_store_cached_summary(conn, content_hash, summary, model)` and `SUMMARY_PROMPT_VERSION` consistent A3↔tests; `read()` returns `text`/`title` keys consumed by `extraction.py`; `fts_source` content table is `source_content` in both `_SEARCH_SCHEMA` and the `ensure_search_schema` backfill map.

**Known follow-ups (not blockers):** the AFTER-UPDATE trigger on `source_content` re-syncs the index on a `summary` write (cheap, summary written once per content); cross-kind bm25 ranking remains "approximately comparable" (queued for Polish); `_source_hits` runs one small subject sub-query per hit (fine at `limit` ≤ 20).
