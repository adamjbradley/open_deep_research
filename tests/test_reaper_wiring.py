"""The preallocate_run node reaps abandoned 'running' rows at the start of each run.

storage.reap_stale_running is already unit-tested (test_reaper.py); this checks the
graph wiring: the node sweeps rows whose last_heartbeat predates the configured
staleness window to status='error', leaves recent/finished rows alone, and still
preallocates the new run. Dependency-free: hits a temp SQLite DB, no LLM/network.
"""
import asyncio

import aiosqlite

from open_deep_research import deep_researcher as dr
from open_deep_research import storage
from open_deep_research.factbase import migrations, schema


def test_preallocate_run_reaps_stale_running_rows(tmp_path):
    db = str(tmp_path / "f.db")

    async def run():
        # Seed: one ancient 'running' row (abandoned), one fresh 'running', one completed.
        async with aiosqlite.connect(db) as conn:
            await storage._ensure_schema(conn)
            await migrations.apply(conn, schema.STEPS)
            await conn.executescript(
                "INSERT INTO research_runs (status, last_heartbeat) VALUES ('running','2000-01-01T00:00:00+00:00');"
                "INSERT INTO research_runs (status, last_heartbeat) VALUES ('running','2999-01-01T00:00:00+00:00');"
                "INSERT INTO research_runs (status, last_heartbeat) VALUES ('completed','2000-01-01T00:00:00+00:00');"
            )
            await conn.commit()

        cfg = {"configurable": {"thread_id": "t-reap", "database_path": db,
                                "run_staleness_minutes": 60}}
        out = await dr.preallocate_run({"messages": []}, cfg)

        # A new run row was preallocated.
        assert "prealloc_run_id" in out

        async with aiosqlite.connect(db) as conn:
            conn.row_factory = aiosqlite.Row
            rows = [dict(r) for r in await (await conn.execute(
                "SELECT status, error, last_heartbeat FROM research_runs ORDER BY id")).fetchall()]

        statuses = [r["status"] for r in rows]
        # ancient running -> reaped to error; fresh running -> untouched; completed -> untouched;
        # plus the brand-new preallocated 'running' row.
        assert statuses.count("error") == 1
        assert statuses.count("completed") == 1
        assert statuses.count("running") == 2  # the future-dated one + the new prealloc
        reaped = [r for r in rows if r["status"] == "error"][0]
        assert reaped["last_heartbeat"].startswith("2000")
        assert "reaped" in (reaped["error"] or "")

    asyncio.run(run())


def test_preallocate_run_skips_reap_when_persistence_off(tmp_path):
    """With persist_results off the node is a no-op (no DB writes, no reap)."""
    db = str(tmp_path / "f2.db")

    async def run():
        async with aiosqlite.connect(db) as conn:
            await storage._ensure_schema(conn)
            await migrations.apply(conn, schema.STEPS)
            await conn.execute(
                "INSERT INTO research_runs (status, last_heartbeat) VALUES ('running','2000-01-01T00:00:00+00:00')")
            await conn.commit()

        cfg = {"configurable": {"thread_id": "t", "database_path": db, "persist_results": False}}
        out = await dr.preallocate_run({"messages": []}, cfg)
        assert out == {}

        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM research_runs WHERE status='running'")
            assert (await cur.fetchone())[0] == 1  # untouched

    asyncio.run(run())


def test_reap_works_on_fresh_db_without_premigration(tmp_path):
    """reap_stale_running must not crash on a DB lacking the last_heartbeat column.

    Regression: the reaper runs at the very start of a run, before the first preallocate
    applies the factbase migrations -- so on a fresh DB last_heartbeat didn't exist yet
    and the reaper errored ('no such column'). It must apply the migrations itself.
    """
    db = str(tmp_path / "fresh.db")

    async def run():
        # Base schema only -- NO factbase migrations (no last_heartbeat column yet).
        async with aiosqlite.connect(db) as conn:
            await storage._ensure_schema(conn)
            await conn.execute("INSERT INTO research_runs (status) VALUES ('running')")
            await conn.commit()

        # Must not raise; nothing to reap (the row's last_heartbeat is NULL after migration).
        n = await storage.reap_stale_running(db, older_than_iso="2099-01-01T00:00:00+00:00")
        assert n == 0

        async with aiosqlite.connect(db) as conn:
            cols = [r[1] for r in await (await conn.execute("PRAGMA table_info(research_runs)")).fetchall()]
            assert "last_heartbeat" in cols  # migration was applied

    asyncio.run(run())
