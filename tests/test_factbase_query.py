import asyncio, aiosqlite
from open_deep_research.factbase import migrations, schema, ingest, profile, entities, registry, query

DI = profile.load("country_digital_identity")

def _ing(conn):
    return ingest.Ingestor(conn, profile=DI, resolver=entities.CountryResolver(),
                           registry=registry.SourceRegistry.load("di_source_registry"))

def _seed(conn):
    return [
        {"property":"id_coverage_pct","instance_name":"India","value":"99","unit":"%","as_of":"2024",
         "qualifiers":{"population_basis":"adults_15plus"},"source_url":"https://id4d.worldbank.org/x","evidence_span":"99%"},
        {"property":"id_coverage_pct","instance_name":"India","value":"87","unit":"%","as_of":"2024",
         "qualifiers":{"population_basis":"adults_15plus"},"source_url":"https://gsma.com/y","evidence_span":"87%"},
    ]

def test_show_returns_facts_with_source_and_conflict():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            await _ing(conn).ingest(run_id=1, records=_seed(conn))
            rows = await query.FactQuery(conn).show("IND")
            assert len(rows) == 2
            assert {r["value"] for r in rows} == {"99", "87"}
            assert all(r["property_name"] == "id_coverage_pct" for r in rows)
            assert all(r["source_url"] for r in rows)            # joined source url present
            assert all(r["in_conflict"] for r in rows)            # both are in the open conflict
            assert all(r["admission"] == "provisional" for r in rows)
    asyncio.run(run())

def test_compare_groups_by_instance():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            await _ing(conn).ingest(run_id=1, records=_seed(conn))
            rows = await query.FactQuery(conn).compare("id_coverage_pct")
            assert all(r["instance_key"] == "IND" for r in rows)
            assert {r["value"] for r in rows} == {"99", "87"}
    asyncio.run(run())
