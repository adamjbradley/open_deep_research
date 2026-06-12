import asyncio, aiosqlite
from open_deep_research.factbase import migrations, schema, store
def test_record_and_read_run_sources():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            s = store.RunSourceStore(conn)
            await s.record("thread-1", "https://x.org/a", "RAW TEXT A", capture_status="raw_text")
            await s.record("thread-1", "https://x.org/a", "RAW TEXT A", capture_status="raw_text")
            await s.record("thread-1", "https://y.org/b", None, capture_status="summarized")
            rows = await s.read("thread-1")
            assert sorted(r["source_url"] for r in rows) == ["https://x.org/a", "https://y.org/b"]
            raw = [r for r in rows if r["capture_status"] == "raw_text"]
            assert raw[0]["text"] == "RAW TEXT A"
    asyncio.run(run())
