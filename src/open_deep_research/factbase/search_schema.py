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
    content='source_content', content_rowid='id'
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_fact USING fts5(
    narrative, value, property_name,
    content='fact', content_rowid='id'
);

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
    # For external-content FTS5, count(*) on the virtual table reads the content
    # table — not the actual index. Use the _docsize shadow table to check how
    # many rows the FTS index has actually indexed.
    # NOTE: counts the FTS5 `_docsize` shadow table — the number of INDEXED rows.
    # `count(*) FROM {fts}` would wrongly route to the external content table and
    # return the content-row count, defeating empty-index detection. `_docsize`
    # exists for all FTS5 tables unless declared with `columnsize=0` — do NOT add
    # `columnsize=0` to _SEARCH_SCHEMA without updating this check.
    cur = await conn.execute(f"SELECT count(*) FROM {fts}_docsize")
    fts_rows = (await cur.fetchone())[0]
    return content_rows > 0 and fts_rows == 0


async def ensure_search_schema(conn: aiosqlite.Connection) -> None:
    """Idempotently create FTS tables + triggers, backfilling empty indexes.

    Issues an implicit COMMIT (via executescript) before creating tables; call at init time, not mid-transaction."""
    # Self-heal: drop a stale run_source-based fts_source so it can be recreated
    # over source_content. executescript commits implicitly, which is fine here.
    cur = await conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='fts_source' AND type='table'")
    row = await cur.fetchone()
    if row and "content='run_source'" in (row[0] or ""):
        await conn.executescript(
            "DROP TRIGGER IF EXISTS run_source_ai; DROP TRIGGER IF EXISTS run_source_ad;"
            " DROP TRIGGER IF EXISTS run_source_au; DROP TABLE IF EXISTS fts_source;")
    await conn.executescript(_SEARCH_SCHEMA)
    for fts, content in (("fts_source", "source_content"), ("fts_fact", "fact")):
        if await _needs_backfill(conn, fts, content):
            await _rebuild(conn, fts)
    await conn.commit()


async def reindex(conn: aiosqlite.Connection) -> None:
    """Force a full rebuild of both FTS indexes from their content tables.

    Issues an implicit COMMIT (via executescript) before creating tables; call at init time, not mid-transaction."""
    await conn.executescript(_SEARCH_SCHEMA)  # ensure tables exist first
    await _rebuild(conn, "fts_source")
    await _rebuild(conn, "fts_fact")
    await conn.commit()
