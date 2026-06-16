import asyncio

import aiosqlite

from open_deep_research.factbase import migrations, schema
from open_deep_research import storage


def test_v6_adds_profile_columns(tmp_path):
    db = str(tmp_path / "fb.db")

    async def go():
        async with aiosqlite.connect(db) as conn:
            await storage._ensure_schema(conn)
            await migrations.apply(conn, schema.STEPS)
            cur = await conn.execute("PRAGMA table_info(research_runs)")
            cols = {r[1] for r in await cur.fetchall()}
            assert {"profile_name", "profile_version", "profile_hash"} <= cols

    asyncio.run(go())


def test_finalize_stamps_profile_fields(tmp_path):
    db = str(tmp_path / "fb.db")

    async def go():
        run_id = await storage.preallocate_run(db, "thread-1")
        await storage.finalize_research_run(db, run_id, {
            "profile_name": "country_digital_identity",
            "profile_version": "1",
            "profile_hash": "abc123",
            "status": "completed",
        })
        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute(
                "SELECT profile_name, profile_version, profile_hash, status FROM research_runs WHERE id=?",
                (run_id,))
            row = await cur.fetchone()
        assert row == ("country_digital_identity", "1", "abc123", "completed")

    asyncio.run(go())


def test_stamp_update_persists(tmp_path):
    # Mirrors the engine's in-connection UPDATE to prove the columns accept a stamp mid-run.
    db = str(tmp_path / "fb.db")

    async def go():
        run_id = await storage.preallocate_run(db, "t")
        async with aiosqlite.connect(db) as conn:
            await migrations.apply(conn, schema.STEPS)
            await conn.execute(
                "UPDATE research_runs SET profile_name=?, profile_version=?, profile_hash=? WHERE id=?",
                ("country_digital_identity", "1", "deadbeef", run_id))
            await conn.commit()
            cur = await conn.execute(
                "SELECT profile_name, profile_hash FROM research_runs WHERE id=?", (run_id,))
            assert await cur.fetchone() == ("country_digital_identity", "deadbeef")

    asyncio.run(go())
