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
            # INSERT a source_content row -> fts_source indexed (triggers are on source_content)
            await conn.execute(
                "INSERT INTO source_content (id, content_hash, source_url, title, text) "
                "VALUES (1,'h1','https://x.org/a','Title A','ROCA vulnerability in Estonia')")
            await conn.commit()
            cur = await conn.execute("SELECT rowid FROM fts_source WHERE fts_source MATCH 'ROCA'")
            assert [r[0] for r in await cur.fetchall()] == [1]
            # UPDATE text -> new term matches, old gone
            await conn.execute("UPDATE source_content SET text='completely different content' WHERE id=1")
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
            # rows exist BEFORE the FTS schema is created (in source_content, not run_source)
            await conn.execute(
                "INSERT INTO source_content (id, content_hash, source_url, title, text) "
                "VALUES (1,'h1','https://x.org/a','T','preexisting biometric text')")
            await conn.commit()
            await search_schema.ensure_search_schema(conn)  # should backfill
            cur = await conn.execute("SELECT count(*) FROM fts_source WHERE fts_source MATCH 'biometric'")
            assert (await cur.fetchone())[0] == 1
            await search_schema.reindex(conn)  # explicit rebuild stays consistent
            cur = await conn.execute("SELECT count(*) FROM fts_source WHERE fts_source MATCH 'biometric'")
            assert (await cur.fetchone())[0] == 1
    asyncio.run(run())


def test_fact_triggers_sync_insert_update():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _migrated_conn(conn)
            await search_schema.ensure_search_schema(conn)
            await conn.execute(
                "INSERT INTO fact (id, instance_key, property_name, value, narrative) "
                "VALUES (1,'EST','data_protection_law','true','Estonia enacted a comprehensive data protection statute')")
            await conn.commit()
            cur = await conn.execute("SELECT rowid FROM fts_fact WHERE fts_fact MATCH 'comprehensive'")
            assert [r[0] for r in await cur.fetchall()] == [1]
            await conn.execute("UPDATE fact SET narrative='now mentions biometric capture' WHERE id=1")
            await conn.commit()
            cur = await conn.execute("SELECT count(*) FROM fts_fact WHERE fts_fact MATCH 'comprehensive'")
            assert (await cur.fetchone())[0] == 0
            cur = await conn.execute("SELECT count(*) FROM fts_fact WHERE fts_fact MATCH 'biometric'")
            assert (await cur.fetchone())[0] == 1
    asyncio.run(run())
