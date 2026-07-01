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
    # subjects.name is a PROMPT SENTENCE (the real-data shape), not a clean country name
    await conn.execute("INSERT INTO subjects (id, slug, name) VALUES "
                       "(1,'x','Research Estonia for the country_digital_identity profile.')")
    await conn.execute("INSERT INTO research_runs (id, subject_id, thread_id) VALUES (1,1,'t1')")
    await conn.execute("INSERT INTO source_content (id, content_hash, source_url, title, text) "
                       "VALUES (1,'h1','https://ria.ee/roca','ROCA','ROCA vulnerability advisory')")
    await conn.execute("INSERT INTO run_source (thread_id, source_url, capture_status, content_hash) "
                       "VALUES ('t1','https://ria.ee/roca','raw_text','h1')")
    await conn.commit()


def test_source_subject_resolves_from_prompt_sentence_name():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            est = await search.search_research(conn, "ROCA", subject="Estonia", kinds=("source",))
            assert est and est[0].subject == "EST"
            deu = await search.search_research(conn, "ROCA", subject="Germany", kinds=("source",))
            assert deu == []
    asyncio.run(run())
