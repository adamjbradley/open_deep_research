import asyncio

import aiosqlite

from open_deep_research.factbase import migrations, schema


def test_schema_creates_all_factbase_tables():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await migrations.apply(conn, schema.STEPS)
            cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {r[0] for r in await cur.fetchall()}
            assert {"run_source", "entity_type", "entity_instance", "unresolved_instance",
                    "property_def", "source", "fact", "evidence", "fact_revision",
                    "conflict", "conflict_member"} <= tables
    asyncio.run(run())


def test_evidence_references_run_source_by_fk():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await migrations.apply(conn, schema.STEPS)
            cur = await conn.execute("PRAGMA foreign_key_list(evidence)")
            fks = await cur.fetchall()
            assert any(row[2] == "run_source" for row in fks)  # row[2] = referenced table
    asyncio.run(run())
