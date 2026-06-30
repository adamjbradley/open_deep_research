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
