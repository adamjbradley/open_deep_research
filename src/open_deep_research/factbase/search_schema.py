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
    # For external-content FTS5, count(*) on the virtual table reads the content
    # table — not the actual index. Use the _docsize shadow table to check how
    # many rows the FTS index has actually indexed.
    cur = await conn.execute(f"SELECT count(*) FROM {fts}_docsize")
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
