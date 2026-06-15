import asyncio, aiosqlite
from open_deep_research import storage
from open_deep_research.factbase import migrations, schema

def test_save_run_updates_preallocated_row_no_duplicate(tmp_path):
    db = str(tmp_path / "f.db")
    async def run():
        async with aiosqlite.connect(db) as conn:
            await storage._ensure_schema(conn)
            await migrations.apply(conn, schema.STEPS)
        rid = await storage.preallocate_run(db, "thread-1")
        run_doc = {"thread_id":"thread-1","topic":"India DI","research_brief":"b","final_report":"r",
                   "sources":[],"raw_notes":[],"config":{},"status":"completed","error":None,
                   "created_at":"2026-06-13T00:00:00Z"}
        sid, returned = await storage.save_run_and_upsert_subject(
            db, subject_name="India", slug="india", merged_report="r",
            sources_union=[], run=run_doc, now="2026-06-13T00:00:00Z", run_id=rid)
        assert returned == rid
        async with aiosqlite.connect(db) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT COUNT(*) AS n FROM research_runs")
            assert (await cur.fetchone())["n"] == 1
            cur = await conn.execute("SELECT status, topic, subject_id FROM research_runs WHERE id=?", (rid,))
            row = await cur.fetchone()
            assert row["status"] == "completed" and row["topic"] == "India DI" and row["subject_id"] == sid
    asyncio.run(run())

def test_save_run_without_run_id_still_inserts(tmp_path):
    db = str(tmp_path / "f.db")
    async def run():
        run_doc = {"thread_id":"t","topic":"T","research_brief":"","final_report":"","sources":[],
                   "raw_notes":[],"config":{},"status":"completed","error":None,"created_at":"2026-06-13T00:00:00Z"}
        sid, rid = await storage.save_run_and_upsert_subject(
            db, subject_name="X", slug="x", merged_report="r", sources_union=[],
            run=run_doc, now="2026-06-13T00:00:00Z")
        assert isinstance(rid, int)
        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM research_runs")
            assert (await cur.fetchone())[0] == 1
    asyncio.run(run())
