# tests/test_qualifier_provenance_migration.py
import asyncio
import aiosqlite
from open_deep_research.factbase import migrations, schema


def test_fact_has_qualifier_provenance_column():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            cur = await conn.execute("PRAGMA table_info(fact)")
            cols = {r[1] for r in await cur.fetchall()}
            assert "qualifier_provenance_json" in cols
    asyncio.run(run())
