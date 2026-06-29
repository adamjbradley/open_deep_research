# tests/test_factbase_search.py
import asyncio
import aiosqlite
from open_deep_research.factbase import schema, migrations, search_schema, search


async def _seed(conn):
    await conn.executescript("""
        CREATE TABLE subjects (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT, name TEXT);
        CREATE TABLE research_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id INTEGER, thread_id TEXT);
    """)
    await conn.commit()
    await migrations.apply(conn, schema.STEPS)
    await search_schema.ensure_search_schema(conn)
    # two subjects: Estonia (EST), Germany (DEU)
    await conn.execute("INSERT INTO subjects (id, slug, name) VALUES (1,'estonia','Estonia')")
    await conn.execute("INSERT INTO subjects (id, slug, name) VALUES (2,'germany','Germany')")
    await conn.execute("INSERT INTO research_runs (id, subject_id, thread_id) VALUES (1,1,'t-est')")
    await conn.execute("INSERT INTO research_runs (id, subject_id, thread_id) VALUES (2,2,'t-deu')")
    # sources
    await conn.execute("INSERT INTO run_source (id, thread_id, source_url, capture_status, text, title) "
                       "VALUES (1,'t-est','https://ria.ee/roca','raw_text','The ROCA vulnerability affected Estonian id-kaart chips','ROCA advisory')")
    await conn.execute("INSERT INTO run_source (id, thread_id, source_url, capture_status, text, title) "
                       "VALUES (2,'t-deu','https://de.gov/eid','raw_text','German eID adoption statistics','German eID')")
    # facts (instance_key is alpha-3)
    await conn.execute("INSERT INTO fact (id, instance_key, property_name, value, narrative, as_of, lifecycle, admission, soft_deleted_at) "
                       "VALUES (1,'EST','id_coverage_pct','98','ROCA-era coverage among adults',2024,'current','trusted',NULL)")
    await conn.commit()


def test_relevance_and_kinds_filter():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            hits = await search.search_research(conn, "ROCA")
            kinds = {(h.kind, h.ref_id) for h in hits}
            assert ("source", 1) in kinds and ("fact", 1) in kinds
            assert ("source", 2) not in kinds
            src_only = await search.search_research(conn, "ROCA", kinds=("source",))
            assert all(h.kind == "source" for h in src_only)
            # snippet highlights the match
            assert any("ROCA" in (h.snippet or "") for h in hits)
    asyncio.run(run())


def test_subject_filter_unifies_alpha3_and_slug():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            # "Estonia" resolves to EST; matches the fact (instance_key=EST) AND
            # the source (thread t-est -> subject Estonia -> resolve -> EST)
            est = await search.search_research(conn, "ROCA", subject="Estonia")
            assert {(h.kind, h.ref_id) for h in est} == {("source", 1), ("fact", 1)}
            assert all(h.subject == "EST" for h in est)
            deu = await search.search_research(conn, "ROCA", subject="Germany")
            assert deu == []
    asyncio.run(run())


def test_metadata_present():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            hits = await search.search_research(conn, "ROCA")
            fact = next(h for h in hits if h.kind == "fact")
            assert (fact.as_of, fact.lifecycle, fact.admission) == (2024, "current", "trusted")
            src = next(h for h in hits if h.kind == "source")
            assert src.source_url == "https://ria.ee/roca" and src.title == "ROCA advisory"
    asyncio.run(run())


def test_softdeleted_excluded():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            await conn.execute("UPDATE fact SET soft_deleted_at='2026-06-29' WHERE id=1")
            await conn.commit()
            hits = await search.search_research(conn, "ROCA")
            assert all(h.kind != "fact" for h in hits)
    asyncio.run(run())


def test_malformed_query_returns_empty_not_raise():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            hits = await search.search_research(conn, '"')   # a bare quote
            assert isinstance(hits, list)  # no exception
    asyncio.run(run())
