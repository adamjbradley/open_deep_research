import asyncio, aiosqlite
from open_deep_research import storage
from open_deep_research.factbase import migrations, schema

def test_preallocate_then_finalize_updates_same_row(tmp_path):
    db = str(tmp_path / "t.db")
    async def run():
        async with aiosqlite.connect(db) as conn:
            await storage._ensure_schema(conn)
            await migrations.apply(conn, schema.STEPS)
        rid = await storage.preallocate_run(db, "thread-7")
        assert isinstance(rid, int)
        await storage.finalize_research_run(db, rid, {"status": "completed", "topic": "X"})
        async with aiosqlite.connect(db) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT status, topic, thread_id FROM research_runs WHERE id=?", (rid,))
            row = await cur.fetchone()
            assert row["status"] == "completed" and row["topic"] == "X" and row["thread_id"] == "thread-7"
            cur = await conn.execute("SELECT COUNT(*) FROM research_runs")
            assert (await cur.fetchone())[0] == 1
    asyncio.run(run())
