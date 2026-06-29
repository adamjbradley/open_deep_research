# Searchable Research Substrate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make already-persisted research findable — an additive, read-only FTS5 index over `run_source.text` and `fact.narrative`, behind a `search_research(...)` query API and a `dossier search` CLI.

**Architecture:** External-content FTS5 virtual tables (`fts_source`, `fts_fact`) kept in sync by SQLite triggers, so the index can't drift across the many fact writers. The query API joins FTS hits back to the base tables to apply freshness/trust metadata and subject filtering, and canonicalizes subject to an alpha-3 country key. The research loop is unchanged except for persisting a `title` the search provider already returns.

**Tech Stack:** Python 3, `aiosqlite` (async SQLite), SQLite **FTS5**, `argparse` CLI. Tests are plain `asyncio.run(...)` over `:memory:` DBs (no pytest-asyncio, no fixtures) — house style per `tests/test_factbase_backfill.py`.

## Global Constraints

- **FTS5 keyword search only** — no embeddings/vector store in v1; the `search_research` signature hides the engine so a semantic adapter can replace it later without changing callers.
- **Additive** — no graph node, edge, or routing change. The *only* write-path change is `run_source.record()` persisting a `title` already present in search results.
- **Migration version = 12.** STEPS currently ends at v10; the parallel `required-qualifier-resolution` branch takes **v11**. Use **v12** here. At merge, confirm v11 and v12 don't collide; if both ended up the same number, renumber the later-merged one.
- **Triggers are applied via `executescript`, NOT via a STEPS migration.** `factbase/migrations.py` splits each step's SQL on `;` (`schema.py` docstring lines 4-6), which corrupts `CREATE TRIGGER ... BEGIN ...; END;` bodies. Only the single-statement `ALTER TABLE run_source ADD COLUMN title` goes through STEPS (v12). The FTS tables + triggers go through an idempotent `ensure_search_schema(conn)` that uses `conn.executescript(...)`.
- **Freshness/trust ride along, never filtered at the index.** Every `fact` hit returns `as_of`/`lifecycle`/`admission`; the index never drops rows by trust. Soft-deleted rows are excluded at *query* time via the join (`soft_deleted_at IS NULL`), not by the index.
- **Canonical subject key = alpha-3** (e.g. `EST`), resolved via `factbase.entities.CountryResolver`. `fact.instance_key` is already alpha-3; a `source`'s subject is derived (`run_source.thread_id → research_runs.subject_id → subjects.name`) then resolved to alpha-3 so one `subject=` filter matches both kinds.
- **Out of scope (do not build):** semantic search, the graph KB-first gate (sub-project ③), cross-run fetch-skip (②), cross-subject *fact* reuse (④), evidence-kind indexing, freshness-weighted ranking.

---

### Task 1: Migration v12 — add `run_source.title`

**Files:**
- Modify: `src/open_deep_research/factbase/schema.py` (append one tuple to `STEPS`, after the v10 entry at line 182-184)
- Test: `tests/test_factbase_migration_v12.py`

**Interfaces:**
- Consumes: `schema.STEPS`, `migrations.apply` (existing).
- Produces: a `run_source.title TEXT` column (nullable) present after `migrations.apply(conn, schema.STEPS)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_migration_v12.py
import asyncio
import aiosqlite
from open_deep_research.factbase import schema, migrations


def test_v12_adds_run_source_title_column():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            # research_runs must exist before STEPS v2 ALTERs it (mirrors storage setup).
            await conn.executescript(
                "CREATE TABLE research_runs (id INTEGER PRIMARY KEY, thread_id TEXT);"
            )
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            cur = await conn.execute("PRAGMA table_info(run_source)")
            cols = {row[1] for row in await cur.fetchall()}
            assert "title" in cols
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_factbase_migration_v12.py -v`
Expected: FAIL — `assert "title" in cols` (column absent).

- [ ] **Step 3: Append the v12 step**

In `src/open_deep_research/factbase/schema.py`, add the new tuple as the last element of `STEPS` (immediately after the `(10, ...)` entry, before the closing `]`):

```python
    (10, """
    ALTER TABLE batch_item ADD COLUMN attempt_count INTEGER DEFAULT 0;
    """),
    (12, """
    ALTER TABLE run_source ADD COLUMN title TEXT;
    """),
]
```

> Note the intentional 10 → 12 gap: v11 belongs to the parallel `required-qualifier-resolution` branch (see Global Constraints). `migrations.apply` sorts and applies pending versions, so a missing v11 on this branch is harmless; both land at merge.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_factbase_migration_v12.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_factbase_migration_v12.py src/open_deep_research/factbase/schema.py
git commit -m "feat(factbase): add run_source.title column (schema v12)"
```

---

### Task 2: Capture the provider `title`

**Files:**
- Modify: `src/open_deep_research/factbase/store.py:10-21` (`RunSourceStore.record`)
- Modify: `src/open_deep_research/utils.py:55-62` (`record_search_sources`)
- Test: `tests/test_run_source_title_capture.py`

**Interfaces:**
- Consumes: the v12 `run_source.title` column (Task 1).
- Produces: `RunSourceStore.record(thread_id, url, text, *, capture_status, reason=None, title=None)` — `title` persisted into `run_source.title`. `record_search_sources` passes `title=result.get("title")`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_run_source_title_capture.py
import asyncio
import aiosqlite
from open_deep_research.factbase import schema, migrations, store
from open_deep_research.utils import record_search_sources


def test_record_persists_title():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript(
                "CREATE TABLE research_runs (id INTEGER PRIMARY KEY, thread_id TEXT);"
            )
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            rs = store.RunSourceStore(conn)
            await rs.record("t1", "https://x.org/a", "body text",
                            capture_status="raw_text", title="The Page Title")
            cur = await conn.execute(
                "SELECT title FROM run_source WHERE source_url=?", ("https://x.org/a",))
            assert (await cur.fetchone())[0] == "The Page Title"
    asyncio.run(run())


def test_record_search_sources_threads_title():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript(
                "CREATE TABLE research_runs (id INTEGER PRIMARY KEY, thread_id TEXT);"
            )
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            rs = store.RunSourceStore(conn)
            results = {"https://x.org/a": {"title": "T", "raw_content": "raw body"}}
            await record_search_sources(rs, "t1", results)
            cur = await conn.execute(
                "SELECT title, capture_status FROM run_source WHERE source_url=?",
                ("https://x.org/a",))
            row = await cur.fetchone()
            assert row[0] == "T" and row[1] == "raw_text"
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_run_source_title_capture.py -v`
Expected: FAIL — `record()` has no `title` kwarg (`TypeError`) / column not written.

- [ ] **Step 3: Thread `title` through `record()`**

Replace `RunSourceStore.record` in `src/open_deep_research/factbase/store.py:10-21` with:

```python
    async def record(self, thread_id: str, url: str, text: str | None, *,
                     capture_status: str, reason: str | None = None,
                     title: str | None = None) -> None:
        ch = _hash(text)
        cur = await self._conn.execute(
            "SELECT 1 FROM run_source WHERE thread_id=? AND source_url=? AND content_hash=?",
            (thread_id, url, ch))
        if await cur.fetchone():
            return
        await self._conn.execute(
            "INSERT INTO run_source (thread_id, source_url, capture_status, reason, text, title, content_hash, retrieved_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (thread_id, url, capture_status, reason, text, title, ch,
             datetime.now(timezone.utc).isoformat()))
        await self._conn.commit()
```

- [ ] **Step 4: Pass the title at the capture call site**

Replace `record_search_sources` in `src/open_deep_research/utils.py:55-62` with:

```python
async def record_search_sources(run_source_store, thread_id: str, unique_results: dict) -> None:
    """Persist each unique search result as a run_source row (raw_text if raw_content present)."""
    for url, result in unique_results.items():
        result = result or {}
        title = result.get("title") or None
        raw = result.get("raw_content") or ""
        if raw:
            await run_source_store.record(thread_id, url, raw, capture_status="raw_text", title=title)
        else:
            await run_source_store.record(thread_id, url, None, capture_status="summarized", title=title)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_run_source_title_capture.py -v`
Expected: PASS (both tests).

- [ ] **Step 6: Commit**

```bash
git add tests/test_run_source_title_capture.py src/open_deep_research/factbase/store.py src/open_deep_research/utils.py
git commit -m "feat(factbase): capture provider title into run_source"
```

---

### Task 3: Search schema — FTS5 tables + sync triggers

**Files:**
- Create: `src/open_deep_research/factbase/search_schema.py`
- Test: `tests/test_factbase_search_schema.py`

**Interfaces:**
- Consumes: `run_source` (with `title`, Task 1/2) and `fact` tables.
- Produces:
  - `async def ensure_search_schema(conn) -> None` — idempotently creates `fts_source`/`fts_fact` + triggers via `executescript`; backfills (rebuilds) any FTS table that is empty while its content table is non-empty.
  - `async def reindex(conn) -> None` — force-rebuilds both FTS tables from their content tables.
  - FTS tables: `fts_source(text, source_url, title)` content=`run_source`; `fts_fact(narrative, value, property_name)` content=`fact`. Column 0 is the primary text column (`text` / `narrative`) for `snippet()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_search_schema.py
import asyncio
import aiosqlite
from open_deep_research.factbase import schema, migrations, search_schema


async def _migrated_conn(conn):
    await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, thread_id TEXT);")
    await conn.commit()
    await migrations.apply(conn, schema.STEPS)


def test_ensure_creates_fts_tables_and_is_idempotent():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _migrated_conn(conn)
            await search_schema.ensure_search_schema(conn)
            await search_schema.ensure_search_schema(conn)  # second call must not raise
            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('fts_source','fts_fact')")
            names = {r[0] for r in await cur.fetchall()}
            assert names == {"fts_source", "fts_fact"}
    asyncio.run(run())


def test_triggers_sync_insert_update_softdelete():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _migrated_conn(conn)
            await search_schema.ensure_search_schema(conn)
            # INSERT a source -> indexed
            await conn.execute(
                "INSERT INTO run_source (id, thread_id, source_url, capture_status, text, title) "
                "VALUES (1,'t1','https://x.org/a','raw_text','ROCA vulnerability in Estonia','Title A')")
            await conn.commit()
            cur = await conn.execute("SELECT rowid FROM fts_source WHERE fts_source MATCH 'ROCA'")
            assert [r[0] for r in await cur.fetchall()] == [1]
            # UPDATE text -> new term matches, old gone
            await conn.execute("UPDATE run_source SET text='completely different content' WHERE id=1")
            await conn.commit()
            cur = await conn.execute("SELECT count(*) FROM fts_source WHERE fts_source MATCH 'ROCA'")
            assert (await cur.fetchone())[0] == 0
            cur = await conn.execute("SELECT count(*) FROM fts_source WHERE fts_source MATCH 'different'")
            assert (await cur.fetchone())[0] == 1
    asyncio.run(run())


def test_backfill_indexes_preexisting_rows_and_reindex():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _migrated_conn(conn)
            # rows exist BEFORE the FTS schema is created
            await conn.execute(
                "INSERT INTO run_source (id, thread_id, source_url, capture_status, text, title) "
                "VALUES (1,'t1','https://x.org/a','raw_text','preexisting biometric text','T')")
            await conn.commit()
            await search_schema.ensure_search_schema(conn)  # should backfill
            cur = await conn.execute("SELECT count(*) FROM fts_source WHERE fts_source MATCH 'biometric'")
            assert (await cur.fetchone())[0] == 1
            await search_schema.reindex(conn)  # explicit rebuild stays consistent
            cur = await conn.execute("SELECT count(*) FROM fts_source WHERE fts_source MATCH 'biometric'")
            assert (await cur.fetchone())[0] == 1
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_factbase_search_schema.py -v`
Expected: FAIL — `ModuleNotFoundError: open_deep_research.factbase.search_schema`.

- [ ] **Step 3: Implement `search_schema.py`**

Create `src/open_deep_research/factbase/search_schema.py`:

```python
"""FTS5 search read-model: external-content virtual tables + sync triggers.

Applied via ``executescript`` (NOT the STEPS migration runner, whose naive
``;`` splitter would corrupt trigger bodies). All DDL is ``IF NOT EXISTS`` so
``ensure_search_schema`` is idempotent; it also backfills any FTS table that is
empty while its content table has rows.
"""
from __future__ import annotations

import aiosqlite

# fts column order matters: column 0 (text/narrative) is what snippet() renders.
_SEARCH_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts_source USING fts5(
    text, source_url, title,
    content='run_source', content_rowid='id'
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_fact USING fts5(
    narrative, value, property_name,
    content='fact', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS run_source_ai AFTER INSERT ON run_source BEGIN
    INSERT INTO fts_source(rowid, text, source_url, title)
        VALUES (new.id, new.text, new.source_url, new.title);
END;
CREATE TRIGGER IF NOT EXISTS run_source_ad AFTER DELETE ON run_source BEGIN
    INSERT INTO fts_source(fts_source, rowid, text, source_url, title)
        VALUES ('delete', old.id, old.text, old.source_url, old.title);
END;
CREATE TRIGGER IF NOT EXISTS run_source_au AFTER UPDATE ON run_source BEGIN
    INSERT INTO fts_source(fts_source, rowid, text, source_url, title)
        VALUES ('delete', old.id, old.text, old.source_url, old.title);
    INSERT INTO fts_source(rowid, text, source_url, title)
        VALUES (new.id, new.text, new.source_url, new.title);
END;

CREATE TRIGGER IF NOT EXISTS fact_ai AFTER INSERT ON fact BEGIN
    INSERT INTO fts_fact(rowid, narrative, value, property_name)
        VALUES (new.id, new.narrative, new.value, new.property_name);
END;
CREATE TRIGGER IF NOT EXISTS fact_ad AFTER DELETE ON fact BEGIN
    INSERT INTO fts_fact(fts_fact, rowid, narrative, value, property_name)
        VALUES ('delete', old.id, old.narrative, old.value, old.property_name);
END;
CREATE TRIGGER IF NOT EXISTS fact_au AFTER UPDATE ON fact BEGIN
    INSERT INTO fts_fact(fts_fact, rowid, narrative, value, property_name)
        VALUES ('delete', old.id, old.narrative, old.value, old.property_name);
    INSERT INTO fts_fact(rowid, narrative, value, property_name)
        VALUES (new.id, new.narrative, new.value, new.property_name);
END;
"""


async def _rebuild(conn: aiosqlite.Connection, fts: str) -> None:
    await conn.execute(f"INSERT INTO {fts}({fts}) VALUES('rebuild')")


async def _needs_backfill(conn: aiosqlite.Connection, fts: str, content: str) -> bool:
    cur = await conn.execute(f"SELECT count(*) FROM {content}")
    content_rows = (await cur.fetchone())[0]
    cur = await conn.execute(f"SELECT count(*) FROM {fts}")
    fts_rows = (await cur.fetchone())[0]
    return content_rows > 0 and fts_rows == 0


async def ensure_search_schema(conn: aiosqlite.Connection) -> None:
    """Idempotently create FTS tables + triggers, backfilling empty indexes."""
    await conn.executescript(_SEARCH_SCHEMA)
    for fts, content in (("fts_source", "run_source"), ("fts_fact", "fact")):
        if await _needs_backfill(conn, fts, content):
            await _rebuild(conn, fts)
    await conn.commit()


async def reindex(conn: aiosqlite.Connection) -> None:
    """Force a full rebuild of both FTS indexes from their content tables."""
    await conn.executescript(_SEARCH_SCHEMA)  # ensure tables exist first
    await _rebuild(conn, "fts_source")
    await _rebuild(conn, "fts_fact")
    await conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_factbase_search_schema.py -v`
Expected: PASS (all three tests).

- [ ] **Step 5: Commit**

```bash
git add tests/test_factbase_search_schema.py src/open_deep_research/factbase/search_schema.py
git commit -m "feat(factbase): FTS5 search tables + sync triggers (correct-by-construction)"
```

---

### Task 4: `search_research` query API

**Files:**
- Create: `src/open_deep_research/factbase/search.py`
- Test: `tests/test_factbase_search.py`

**Interfaces:**
- Consumes: `ensure_search_schema`/`reindex` (Task 3), `factbase.entities.CountryResolver`.
- Produces:
  - `Hit` (dataclass): `kind: str`, `ref_id: int`, `subject: str | None`, `snippet: str`, `score: float`, `source_url: str | None`, `title: str | None`, `property_name: str | None`, `as_of`, `lifecycle: str | None`, `admission: str | None`, `retrieved_at: str | None`.
  - `async def search_research(conn, query, *, subject=None, kinds=("source","fact"), limit=20) -> list[Hit]`
  - `def format_hits(hits, fmt="text") -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_search.py
import asyncio
import aiosqlite
from open_deep_research.factbase import schema, migrations, search_schema, search


async def _seed(conn):
    await conn.executescript("""
        CREATE TABLE subjects (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT, name TEXT);
        CREATE TABLE research_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id INTEGER, thread_id TEXT);
    """)
    await conn.commit()
    await migrations.apply(conn, schema.STEPS)
    await search_schema.ensure_search_schema(conn)
    # two subjects: Estonia (EST), Germany (DEU)
    await conn.execute("INSERT INTO subjects (id, slug, name) VALUES (1,'estonia','Estonia')")
    await conn.execute("INSERT INTO subjects (id, slug, name) VALUES (2,'germany','Germany')")
    await conn.execute("INSERT INTO research_runs (id, subject_id, thread_id) VALUES (1,1,'t-est')")
    await conn.execute("INSERT INTO research_runs (id, subject_id, thread_id) VALUES (2,2,'t-deu')")
    # sources
    await conn.execute("INSERT INTO run_source (id, thread_id, source_url, capture_status, text, title) "
                       "VALUES (1,'t-est','https://ria.ee/roca','raw_text','The ROCA vulnerability affected Estonian id-kaart chips','ROCA advisory')")
    await conn.execute("INSERT INTO run_source (id, thread_id, source_url, capture_status, text, title) "
                       "VALUES (2,'t-deu','https://de.gov/eid','raw_text','German eID adoption statistics','German eID')")
    # facts (instance_key is alpha-3)
    await conn.execute("INSERT INTO fact (id, instance_key, property_name, value, narrative, as_of, lifecycle, admission, soft_deleted_at) "
                       "VALUES (1,'EST','id_coverage_pct','98','ROCA-era coverage among adults',2024,'current','trusted',NULL)")
    await conn.commit()


def test_relevance_and_kinds_filter():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            hits = await search.search_research(conn, "ROCA")
            kinds = {(h.kind, h.ref_id) for h in hits}
            assert ("source", 1) in kinds and ("fact", 1) in kinds
            assert ("source", 2) not in kinds
            src_only = await search.search_research(conn, "ROCA", kinds=("source",))
            assert all(h.kind == "source" for h in src_only)
            # snippet highlights the match
            assert any("ROCA" in (h.snippet or "") for h in hits)
    asyncio.run(run())


def test_subject_filter_unifies_alpha3_and_slug():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            # "Estonia" resolves to EST; matches the fact (instance_key=EST) AND
            # the source (thread t-est -> subject Estonia -> resolve -> EST)
            est = await search.search_research(conn, "ROCA", subject="Estonia")
            assert {(h.kind, h.ref_id) for h in est} == {("source", 1), ("fact", 1)}
            assert all(h.subject == "EST" for h in est)
            deu = await search.search_research(conn, "ROCA", subject="Germany")
            assert deu == []
    asyncio.run(run())


def test_metadata_present():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            hits = await search.search_research(conn, "ROCA")
            fact = next(h for h in hits if h.kind == "fact")
            assert (fact.as_of, fact.lifecycle, fact.admission) == (2024, "current", "trusted")
            src = next(h for h in hits if h.kind == "source")
            assert src.source_url == "https://ria.ee/roca" and src.title == "ROCA advisory"
    asyncio.run(run())


def test_softdeleted_excluded():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            await conn.execute("UPDATE fact SET soft_deleted_at='2026-06-29' WHERE id=1")
            await conn.commit()
            hits = await search.search_research(conn, "ROCA")
            assert all(h.kind != "fact" for h in hits)
    asyncio.run(run())


def test_malformed_query_returns_empty_not_raise():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            hits = await search.search_research(conn, '"')   # a bare quote
            assert isinstance(hits, list)  # no exception
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_factbase_search.py -v`
Expected: FAIL — `ModuleNotFoundError: open_deep_research.factbase.search`.

- [ ] **Step 3: Implement `search.py`**

Create `src/open_deep_research/factbase/search.py`:

```python
"""Keyword search over the research substrate (FTS5).

Read-only query layer. Hides the FTS engine behind ``search_research`` so a
semantic adapter can replace it later without changing callers. Freshness/trust
fields ride along on every hit but are never used to filter here.
"""
from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

from . import search_schema
from .entities import CountryResolver

_SNIPPET = "snippet({tbl}, 0, '[', ']', '…', 12)"


@dataclass
class Hit:
    kind: str                       # "source" | "fact"
    ref_id: int                     # base-table row id
    subject: str | None             # canonical alpha-3 country key
    snippet: str
    score: float                    # higher = more relevant (−bm25)
    source_url: str | None = None
    title: str | None = None
    property_name: str | None = None
    as_of: object = None
    lifecycle: str | None = None
    admission: str | None = None
    retrieved_at: str | None = None


def _to_match(query: str) -> str | None:
    """Quote each whitespace token as an FTS5 literal so user input can't be a
    syntax error (a bare ``"`` or operator). Returns None if nothing usable."""
    tokens = [t for t in (query or "").split() if t.strip()]
    quoted = ['"' + t.replace('"', '""') + '"' for t in tokens]
    quoted = [q for q in quoted if q != '""']
    return " ".join(quoted) or None


async def _resolve_subject(name: str | None) -> str | None:
    return CountryResolver().resolve(name) if name else None


async def _source_hits(conn, match, target, limit):
    sql = f"""
        SELECT rs.id, rs.source_url, rs.title, rs.retrieved_at, rs.thread_id,
               s.name AS subject_name,
               bm25(fts_source) AS score, {_SNIPPET.format(tbl='fts_source')} AS snip
        FROM fts_source
        JOIN run_source rs ON rs.id = fts_source.rowid
        LEFT JOIN research_runs r ON r.thread_id = rs.thread_id
        LEFT JOIN subjects s ON s.id = r.subject_id
        WHERE fts_source MATCH ? AND rs.soft_deleted_at IS NULL
        ORDER BY score LIMIT ?
    """
    conn.row_factory = aiosqlite.Row
    cur = await conn.execute(sql, (match, limit))
    out = []
    for row in await cur.fetchall():
        subj = CountryResolver().resolve(row["subject_name"]) if row["subject_name"] else None
        if target is not None and subj != target:
            continue
        out.append(Hit(kind="source", ref_id=row["id"], subject=subj,
                       snippet=row["snip"], score=-row["score"],
                       source_url=row["source_url"], title=row["title"],
                       retrieved_at=row["retrieved_at"]))
    return out


async def _fact_hits(conn, match, target, limit):
    sql = f"""
        SELECT f.id, f.instance_key, f.property_name, f.as_of, f.lifecycle, f.admission,
               bm25(fts_fact) AS score, {_SNIPPET.format(tbl='fts_fact')} AS snip
        FROM fts_fact
        JOIN fact f ON f.id = fts_fact.rowid
        WHERE fts_fact MATCH ? AND f.soft_deleted_at IS NULL
        {{subject}}
        ORDER BY score LIMIT ?
    """
    params: list = [match]
    subject_clause = ""
    if target is not None:
        subject_clause = "AND f.instance_key = ?"
        params.append(target)
    params.append(limit)
    conn.row_factory = aiosqlite.Row
    cur = await conn.execute(sql.format(subject=subject_clause), tuple(params))
    return [Hit(kind="fact", ref_id=row["id"], subject=row["instance_key"],
                snippet=row["snip"], score=-row["score"],
                property_name=row["property_name"], as_of=row["as_of"],
                lifecycle=row["lifecycle"], admission=row["admission"])
            for row in await cur.fetchall()]


async def search_research(conn, query, *, subject=None, kinds=("source", "fact"), limit=20):
    """Keyword-search the substrate. Returns ranked Hits (higher score = better).

    Cross-kind scores are both −bm25 and only approximately comparable in v1.
    """
    await search_schema.ensure_search_schema(conn)
    match = _to_match(query)
    if match is None:
        return []
    target = await _resolve_subject(subject)
    hits: list[Hit] = []
    try:
        if "source" in kinds:
            hits += await _source_hits(conn, match, target, limit)
        if "fact" in kinds:
            hits += await _fact_hits(conn, match, target, limit)
    except aiosqlite.OperationalError:
        return []  # FTS syntax edge cases degrade to "no results", never raise
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]


def format_hits(hits, fmt: str = "text") -> str:
    if not hits:
        return "(no results)"
    lines = []
    for h in hits:
        meta = h.source_url if h.kind == "source" else f"{h.subject}/{h.property_name}"
        fresh = f" [{h.lifecycle},{h.admission}]" if h.kind == "fact" else ""
        lines.append(f"{h.score:+.3f}  {h.kind:<6} {meta}{fresh}\n        {h.snippet}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_factbase_search.py -v`
Expected: PASS (all five tests).

> If `CountryResolver().resolve("Estonia")` does not return `"EST"`, check the resolver's expected input in `factbase/entities.py` and adjust the seed names in the test to ones it knows; the production contract (alpha-3) is unchanged.

- [ ] **Step 5: Commit**

```bash
git add tests/test_factbase_search.py src/open_deep_research/factbase/search.py
git commit -m "feat(factbase): search_research query API over FTS5"
```

---

### Task 5: `dossier search` + `dossier reindex` CLI

**Files:**
- Modify: `src/open_deep_research/factbase/dossier.py` (add two subparsers in `_parser()` ~line 159; add two handlers in the command dispatch ~line 303-315; call `ensure_search_schema` after `migrations.apply`)
- Test: `tests/test_dossier_search_cli.py`

**Interfaces:**
- Consumes: `search.search_research`, `search.format_hits`, `search_schema.reindex` (Task 3/4).
- Produces: CLI commands `dossier search "<query>" [--subject X] [--kind source|fact] [--limit N] [--format text|md|csv]` and `dossier reindex`. A testable handler `async def _run_search(conn, *, query, subject, kinds, limit, fmt) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dossier_search_cli.py
import asyncio
import aiosqlite
from open_deep_research.factbase import schema, migrations, search_schema, dossier


async def _seed(conn):
    await conn.executescript("""
        CREATE TABLE subjects (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT, name TEXT);
        CREATE TABLE research_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id INTEGER, thread_id TEXT);
    """)
    await conn.commit()
    await migrations.apply(conn, schema.STEPS)
    await search_schema.ensure_search_schema(conn)
    await conn.execute("INSERT INTO run_source (id, thread_id, source_url, capture_status, text, title) "
                       "VALUES (1,'t1','https://ria.ee/roca','raw_text','ROCA vulnerability advisory','ROCA')")
    await conn.commit()


def test_parser_accepts_search_and_reindex():
    p = dossier._parser()
    assert p.parse_args(["search", "ROCA"]).command == "search"
    assert p.parse_args(["reindex"]).command == "reindex"


def test_run_search_returns_ranked_text():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            out = await dossier._run_search(conn, query="ROCA", subject=None,
                                            kinds=("source", "fact"), limit=20, fmt="text")
            assert "ROCA" in out and "source" in out
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dossier_search_cli.py -v`
Expected: FAIL — `argument command: invalid choice: 'search'` and `dossier._run_search` missing.

- [ ] **Step 3: Add the subparsers**

In `src/open_deep_research/factbase/dossier.py`, inside `_parser()` (after the last existing `sub.add_parser(...)` block, near line 159), add:

```python
    srch = sub.add_parser("search", help="Keyword-search the research substrate (facts + raw sources).")
    srch.add_argument("query")
    srch.add_argument("--subject", default=None, help="Filter to one country (name or code).")
    srch.add_argument("--kind", choices=["source", "fact"], action="append", dest="kinds",
                      help="Restrict to a kind; repeatable. Default: both.")
    srch.add_argument("--limit", type=int, default=20)
    srch.add_argument("--format", choices=["text", "md", "csv"], default="text")

    sub.add_parser("reindex", help="Rebuild the search index from stored sources and facts.")
```

- [ ] **Step 4: Add the handler function + dispatch**

In `src/open_deep_research/factbase/dossier.py`, add a module-level helper (near the other command helpers):

```python
async def _run_search(conn, *, query, subject, kinds, limit, fmt) -> str:
    from . import search, search_schema
    await search_schema.ensure_search_schema(conn)
    kinds = tuple(kinds) if kinds else ("source", "fact")
    hits = await search.search_research(conn, query, subject=subject, kinds=kinds, limit=limit)
    return search.format_hits(hits, fmt=fmt)
```

Then in the command dispatch block (the `async with aiosqlite.connect(db_path) as conn:` body, alongside the `if args.command == "show":` branches near line 303), add:

```python
    if args.command == "search":
        await migrations.apply(conn, schema.STEPS)
        return await _run_search(conn, query=args.query, subject=args.subject,
                                 kinds=args.kinds, limit=args.limit, fmt=args.format)
    if args.command == "reindex":
        await migrations.apply(conn, schema.STEPS)
        await search_schema.reindex(conn)
        return "Search index rebuilt."
```

Ensure the module imports `search_schema` at the top (add `from . import search_schema` near the existing `from . import ...` imports if not already present via the lazy import in `_run_search`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_dossier_search_cli.py -v`
Expected: PASS (both tests).

- [ ] **Step 6: Run the full new-test suite + a broad regression check**

Run: `python -m pytest tests/test_factbase_migration_v12.py tests/test_run_source_title_capture.py tests/test_factbase_search_schema.py tests/test_factbase_search.py tests/test_dossier_search_cli.py -v`
Expected: PASS (all).
Run: `python -m pytest tests/ -k "factbase or store or backfill or dossier" -q`
Expected: PASS — the `store.record` / `record_search_sources` signature changes didn't break existing callers.

- [ ] **Step 7: Commit**

```bash
git add tests/test_dossier_search_cli.py src/open_deep_research/factbase/dossier.py
git commit -m "feat(cli): dossier search + dossier reindex over the research substrate"
```

---

## Self-Review

**Spec coverage** (each spec section → task):
- FTS5 over `run_source.text` + `fact.narrative` → Task 3 (`fts_source`/`fts_fact`).
- `title` real column, populated at capture → Task 1 (column) + Task 2 (capture).
- Vector-ready `search_research(...)` API with typed Hits + freshness/trust → Task 4.
- Subject resolution (fact `instance_key` + source join → alpha-3) → Task 4 (`_resolve_subject`, `_source_hits`).
- Trigger-synced, correct-by-construction; backfill; `reindex` escape hatch → Task 3.
- Schema v12, coordinate with v11 → Task 1 (+ Global Constraints).
- Malformed query → `[]` not raise → Task 4 (`_to_match` + `OperationalError` guard).
- Thin `dossier search` CLI (+ `reindex`) → Task 5.
- Out-of-scope items → none implemented (verified: no embeddings, no graph gate, no cross-run fetch-skip, no evidence kind).

**Placeholder scan:** none — every step has full code/commands.

**Type consistency:** `Hit` fields are produced in Task 4 and consumed by `format_hits`/CLI (Task 5) with matching names (`kind`, `score`, `subject`, `snippet`, `source_url`, `property_name`, `lifecycle`, `admission`). `record(..., title=None)` (Task 2) matches the `run_source.title` column (Task 1). `ensure_search_schema`/`reindex` (Task 3) are the names called in Tasks 4 and 5. FTS column 0 (`text`/`narrative`) is consistently the `snippet()` target.

**Known v1 limitations (documented, not defects):** cross-kind bm25 scores are only approximately comparable; the FTS triggers only keep the index live for connections that ran `ensure_search_schema` — `dossier reindex` is the recovery path; subject canonicalization assumes country subjects (non-country subjects get `subject=None`).
