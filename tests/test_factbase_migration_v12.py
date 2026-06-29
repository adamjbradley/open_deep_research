import asyncio
import aiosqlite
from open_deep_research.factbase import schema, migrations


def test_v12_adds_run_source_title_column():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            # research_runs must exist before STEPS v2 ALTERs it (mirrors storage setup).
            await conn.executescript(
                "CREATE TABLE research_runs (id INTEGER PRIMARY KEY, thread_id TEXT);"
            )
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            cur = await conn.execute("PRAGMA table_info(run_source)")
            cols = {row[1] for row in await cur.fetchall()}
            assert "title" in cols
    asyncio.run(run())
