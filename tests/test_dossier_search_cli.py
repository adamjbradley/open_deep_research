"""Tests for `dossier search` and `dossier reindex` CLI commands (Task 5)."""
import asyncio
import aiosqlite
from open_deep_research.factbase import schema, migrations, search_schema, dossier


async def _seed(conn):
    await conn.executescript("""
        CREATE TABLE subjects (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT, name TEXT);
        CREATE TABLE research_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id INTEGER, thread_id TEXT);
    """)
    await conn.commit()
    await migrations.apply(conn, schema.STEPS)
    await search_schema.ensure_search_schema(conn)
    await conn.execute("INSERT INTO run_source (id, thread_id, source_url, capture_status, text, title) "
                       "VALUES (1,'t1','https://ria.ee/roca','raw_text','ROCA vulnerability advisory','ROCA')")
    await conn.commit()


def test_parser_accepts_search_and_reindex():
    p = dossier._parser()
    assert p.parse_args(["search", "ROCA"]).command == "search"
    assert p.parse_args(["reindex"]).command == "reindex"


def test_run_search_returns_ranked_text():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            out = await dossier._run_search(conn, query="ROCA", subject=None,
                                            kinds=("source", "fact"), limit=20, fmt="text")
            assert "ROCA" in out and "source" in out
    asyncio.run(run())


def test_run_search_csv_format():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            out = await dossier._run_search(conn, query="ROCA", subject=None,
                                            kinds=("source", "fact"), limit=20, fmt="csv")
            lines = out.splitlines()
            assert lines[0] == "kind,subject,score,detail,snippet"
            # at least one data row contains the source URL
            assert any("ria.ee" in line or "ROCA" in line for line in lines[1:])
    asyncio.run(run())


def test_run_search_md_format():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            out = await dossier._run_search(conn, query="ROCA", subject=None,
                                            kinds=("source", "fact"), limit=20, fmt="md")
            assert "| score | kind |" in out
    asyncio.run(run())
