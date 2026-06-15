import asyncio, aiosqlite
from open_deep_research import storage
from open_deep_research.factbase import migrations, schema


def test_set_coverage_incomplete(tmp_path):
    db = str(tmp_path / "f.db")
    async def run():
        async with aiosqlite.connect(db) as conn:
            await storage._ensure_schema(conn)
            await migrations.apply(conn, schema.STEPS)
        rid = await storage.preallocate_run(db, "t1")
        await storage.set_coverage_incomplete(db, rid, True)
        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute("SELECT coverage_incomplete FROM research_runs WHERE id=?", (rid,))
            assert (await cur.fetchone())[0] == 1
    asyncio.run(run())
