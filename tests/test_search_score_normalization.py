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
    # two facts + one source all matching "identity", different bm25
    await conn.execute("INSERT INTO fact (id, instance_key, property_name, value, narrative, soft_deleted_at) "
                       "VALUES (1,'EST','p','v','identity card scheme national identity',NULL)")
    await conn.execute("INSERT INTO fact (id, instance_key, property_name, value, narrative, soft_deleted_at) "
                       "VALUES (2,'EST','q','v','identity',NULL)")
    await conn.execute("INSERT INTO source_content (id, content_hash, source_url, title, text) "
                       "VALUES (1,'h','http://x','T','national digital identity wallet identity')")
    await conn.commit()


def test_scores_normalized_to_unit_range_per_kind():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            hits = await search.search_research(conn, "identity")
            assert hits
            assert all(0.0 <= h.score <= 1.0 for h in hits)          # normalized
            assert max(h.score for h in hits) == 1.0                 # top of each kind is 1.0
            # sorted descending by normalized score
            assert hits == sorted(hits, key=lambda h: h.score, reverse=True)
    asyncio.run(run())


def test_single_hit_kind_normalizes_to_one():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            hits = await search.search_research(conn, "wallet", kinds=("source",))
            assert len(hits) == 1 and hits[0].score == 1.0
    asyncio.run(run())
