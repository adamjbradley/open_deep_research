import asyncio, aiosqlite
from open_deep_research.factbase import schema, migrations, search_schema, search, store


async def _prep(conn):
    await conn.executescript("""
        CREATE TABLE subjects (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT, name TEXT);
        CREATE TABLE research_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id INTEGER, thread_id TEXT);
    """)
    await conn.commit()
    await migrations.apply(conn, schema.STEPS)
    await search_schema.ensure_search_schema(conn)


def test_search_restores_row_factory():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _prep(conn)
            conn.row_factory = None
            await search.search_research(conn, "anything")
            assert conn.row_factory is None            # restored, not left as aiosqlite.Row
    asyncio.run(run())


def test_read_restores_row_factory():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _prep(conn)
            conn.row_factory = None
            await store.RunSourceStore(conn).read("t1")
            assert conn.row_factory is None
    asyncio.run(run())
