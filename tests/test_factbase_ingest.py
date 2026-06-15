import asyncio

import aiosqlite

from open_deep_research.factbase import entities, identity, ingest, migrations, profile, registry, schema

DI = profile.load("country_digital_identity")


def _setup(conn):
    return ingest.Ingestor(conn, profile=DI, resolver=entities.CountryResolver(),
                           registry=registry.SourceRegistry.load("di_source_registry"))


def test_two_conflicting_trust_bar_facts_open_conflict_and_stay_provisional():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            ing = _setup(conn)
            recs = [
                {"property": "id_coverage_pct", "instance_name": "India", "value": "99", "unit": "%", "as_of": "2024",
                 "qualifiers": {"population_basis": "adults_15plus"}, "source_url": "https://id4d.worldbank.org/x", "evidence_span": "99%"},
                {"property": "id_coverage_pct", "instance_name": "India", "value": "87", "unit": "%", "as_of": "2024",
                 "qualifiers": {"population_basis": "adults_15plus"}, "source_url": "https://gsma.com/y", "evidence_span": "87%"},
            ]
            await ing.ingest(run_id=1, records=recs)
            cur = await conn.execute("SELECT COUNT(*) FROM fact"); assert (await cur.fetchone())[0] == 2
            cur = await conn.execute("SELECT COUNT(*) FROM conflict WHERE status='open'"); assert (await cur.fetchone())[0] == 1
            cur = await conn.execute("SELECT COUNT(*) FROM fact WHERE admission='trusted'"); assert (await cur.fetchone())[0] == 0
            cur = await conn.execute("SELECT COUNT(*) FROM evidence"); assert (await cur.fetchone())[0] == 2
    asyncio.run(run())


def test_single_trusted_source_promotes():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            ing = _setup(conn)
            recs = [{"property": "id_coverage_pct", "instance_name": "India", "value": "99", "unit": "%", "as_of": "2024",
                     "qualifiers": {"population_basis": "adults_15plus"}, "source_url": "https://id4d.worldbank.org/x", "evidence_span": "99%"}]
            await ing.ingest(run_id=1, records=recs)
            cur = await conn.execute("SELECT admission FROM fact"); assert (await cur.fetchone())[0] == "trusted"
    asyncio.run(run())


def test_unresolved_instance_quarantined_not_a_fact():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            ing = _setup(conn)
            recs = [{"property": "id_coverage_pct", "instance_name": "Atlantis", "value": "50", "unit": "%", "as_of": "2024",
                     "qualifiers": {"population_basis": "adults_15plus"}, "source_url": "https://id4d.worldbank.org/x", "evidence_span": "50%"}]
            await ing.ingest(run_id=1, records=recs)
            cur = await conn.execute("SELECT COUNT(*) FROM fact"); assert (await cur.fetchone())[0] == 0
            cur = await conn.execute("SELECT COUNT(*) FROM unresolved_instance"); assert (await cur.fetchone())[0] == 1
    asyncio.run(run())


def test_fact_row_stores_instance_property_and_qualifiers():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            ing = _setup(conn)
            recs = [{"property": "id_coverage_pct", "instance_name": "India", "value": "99", "unit": "%", "as_of": "2024",
                     "qualifiers": {"population_basis": "adults_15plus"},
                     "source_url": "https://id4d.worldbank.org/x", "evidence_span": "99%"}]
            await ing.ingest(run_id=1, records=recs)
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT instance_key, property_name, qualifiers_json FROM fact")
            row = await cur.fetchone()
            assert row["instance_key"] == "IND"
            assert row["property_name"] == "id_coverage_pct"
            import json
            assert json.loads(row["qualifiers_json"]).get("population_basis") == "adults_15plus"
    asyncio.run(run())


def test_tuple_key_uses_stable_canonical_key():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            ing = _setup(conn)
            recs = [{"property": "id_coverage_pct", "instance_name": "India", "value": "99", "unit": "%", "as_of": "2024",
                     "qualifiers": {"population_basis": "adults_15plus"}, "source_url": "https://id4d.worldbank.org/x", "evidence_span": "99%"}]
            await ing.ingest(run_id=1, records=recs)
            cur = await conn.execute("SELECT tuple_key FROM fact")
            stored = (await cur.fetchone())[0]
            expected = identity.tuple_key(
                "IND", "id_coverage_pct",
                {"population_basis": "adults_15plus", "coverage_kind": None, "measured_modeled": None},
            )
            assert stored == expected
    asyncio.run(run())


def test_promotes_with_required_qualifier_even_if_refinements_missing():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            ing = _setup(conn)
            recs = [{"property":"id_coverage_pct","instance_name":"India","value":"99","unit":"%","as_of":"2024",
                     "qualifiers":{"population_basis":"adults_15plus"},
                     "source_url":"https://id4d.worldbank.org/x","evidence_span":"99%"}]
            await ing.ingest(run_id=1, records=recs)
            cur = await conn.execute("SELECT admission FROM fact"); assert (await cur.fetchone())[0] == "trusted"
    asyncio.run(run())


def test_no_promote_when_required_qualifier_missing():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            ing = _setup(conn)
            recs = [{"property":"id_coverage_pct","instance_name":"India","value":"99","unit":"%","as_of":"2024",
                     "qualifiers":{"coverage_kind":"enrolled"},
                     "source_url":"https://id4d.worldbank.org/x","evidence_span":"99%"}]
            await ing.ingest(run_id=1, records=recs)
            cur = await conn.execute("SELECT admission FROM fact"); assert (await cur.fetchone())[0] == "provisional"
    asyncio.run(run())
