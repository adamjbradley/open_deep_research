# tests/test_fts_source_content.py
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
    await conn.execute("INSERT INTO subjects (id, slug, name) VALUES (1,'estonia','Estonia')")
    await conn.execute("INSERT INTO research_runs (id, subject_id, thread_id) VALUES (1,1,'t-est')")
    # same content captured by two runs (would be 2 hits pre-dedup) -> one source_content row
    await conn.execute("INSERT INTO source_content (id, content_hash, source_url, title, text) "
                       "VALUES (1,'h1','http://roca','ROCA','The ROCA vulnerability in Estonian id-kaart')")
    await conn.execute("INSERT INTO run_source (thread_id, source_url, capture_status, content_hash) "
                       "VALUES ('t-est','http://roca','raw_text','h1')")
    await conn.execute("INSERT INTO run_source (thread_id, source_url, capture_status, content_hash) "
                       "VALUES ('t-other','http://roca','raw_text','h1')")
    await conn.commit()


def test_one_hit_per_content_and_subject_via_capture():
    async def run():
        conn = await aiosqlite.connect(":memory:")
        await _seed(conn)
        hits = await search.search_research(conn, "ROCA", kinds=("source",))
        assert len(hits) == 1 and hits[0].ref_id == 1            # deduped
        assert hits[0].subject == "EST"                          # via the t-est capture
        assert (await search.search_research(conn, "ROCA", subject="Estonia", kinds=("source",)))
        assert await search.search_research(conn, "ROCA", subject="Germany", kinds=("source",)) == []
        await conn.close()
    asyncio.run(run())
