import asyncio, aiosqlite
from open_deep_research import storage
from open_deep_research.factbase import migrations, schema
def test_reaper_marks_old_running_as_error(tmp_path):
    db = str(tmp_path / "f.db")
    async def run():
        async with aiosqlite.connect(db) as conn:
            await storage._ensure_schema(conn)
            await migrations.apply(conn, schema.STEPS)
            await conn.executescript(
                "INSERT INTO research_runs (status, last_heartbeat) VALUES ('running','2000-01-01T00:00:00Z');"
                "INSERT INTO research_runs (status, last_heartbeat) VALUES ('running','2999-01-01T00:00:00Z');"
                "INSERT INTO research_runs (status, last_heartbeat) VALUES ('completed','2000-01-01T00:00:00Z');")
            await conn.commit()
        n = await storage.reap_stale_running(db, older_than_iso="2026-06-13T00:00:00Z")
        assert n == 1
        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM research_runs WHERE status='running'")
            assert (await cur.fetchone())[0] == 1
            cur = await conn.execute("SELECT COUNT(*) FROM research_runs WHERE status='error'")
            assert (await cur.fetchone())[0] == 1
    asyncio.run(run())
