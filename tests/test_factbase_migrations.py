import asyncio

import aiosqlite

from open_deep_research.factbase import migrations


def test_apply_runs_pending_migrations_once():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            steps = [
                (1, "CREATE TABLE a (id INTEGER PRIMARY KEY);"),
                (2, "CREATE TABLE b (id INTEGER PRIMARY KEY);"),
            ]
            await migrations.apply(conn, steps)
            await migrations.apply(conn, steps)  # idempotent re-apply
            cur = await conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
            assert [r[0] for r in await cur.fetchall()] == [1, 2]
            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('a','b')"
            )
            assert {r[0] for r in await cur.fetchall()} == {"a", "b"}

    asyncio.run(run())
