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
                "https://b.org/2": {"raw_content": ""},
            }
            await utils.record_search_sources(store.RunSourceStore(conn), "thread-9", unique)
            rows = await store.RunSourceStore(conn).read("thread-9")
            by_url = {r["source_url"]: r for r in rows}
            assert by_url["https://a.org/1"]["capture_status"] == "raw_text"
            assert by_url["https://a.org/1"]["text"] == "RAW A"
            assert by_url["https://b.org/2"]["capture_status"] == "summarized"
            assert by_url["https://b.org/2"]["text"] is None
    asyncio.run(run())
