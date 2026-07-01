import asyncio, aiosqlite
from open_deep_research.factbase import schema, migrations, search_schema, search


async def _seed(conn):
    await conn.executescript("""
        CREATE TABLE subjects (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT, name TEXT);
        CREATE TABLE research_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id INTEGER, thread_id TEXT);
    """)
    await conn.commit()
    await migrations.apply(conn, schema.STEPS)
    await search_schema.ensure_search_schema(conn)
    # a fact whose value/property match "coverage" but with NULL narrative -> empty snippet
    await conn.execute("INSERT INTO fact (id, instance_key, property_name, value, narrative, "
                       "admission, lifecycle, soft_deleted_at) "
                       "VALUES (1,'EST','id_coverage_pct','98',NULL,'trusted','current',NULL)")
    await conn.commit()


def test_fact_hit_falls_back_to_value_when_snippet_empty():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            hits = await search.search_research(conn, "id_coverage_pct", kinds=("fact",))
            assert hits and hits[0].value == "98"
            out = search.format_hits(hits, "text")
            assert "id_coverage_pct = 98" in out
            assert "None" not in out
    asyncio.run(run())
