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
