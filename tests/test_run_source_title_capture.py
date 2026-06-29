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
