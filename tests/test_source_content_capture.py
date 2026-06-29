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
