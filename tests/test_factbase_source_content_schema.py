import asyncio, aiosqlite
from open_deep_research.factbase import schema, migrations


def test_v13_creates_source_content():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, thread_id TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            cur = await conn.execute("PRAGMA table_info(source_content)")
            cols = {r[1] for r in await cur.fetchall()}
            assert {"content_hash", "text", "summary", "summary_model",
                    "summary_prompt_version"} <= cols
            # content_hash is UNIQUE
            await conn.execute("INSERT INTO source_content (content_hash, text) VALUES ('h','t')")
            try:
                await conn.execute("INSERT INTO source_content (content_hash, text) VALUES ('h','t2')")
                raised = False
            except aiosqlite.IntegrityError:
                raised = True
            assert raised
    asyncio.run(run())
