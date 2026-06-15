import asyncio, aiosqlite
from open_deep_research.factbase import migrations, schema, ingest, profile, entities, registry, metrics

DI = profile.load("country_digital_identity")


def test_metrics_counts(tmp_path):
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            ing = ingest.Ingestor(conn, profile=DI, resolver=entities.CountryResolver(),
                                  registry=registry.SourceRegistry.load("di_source_registry"))
            # India: one trusted (id4d, no conflict); Estonia: two conflicting (both provisional)
            await ing.ingest(run_id=1, records=[
                {"property":"id_coverage_pct","instance_name":"India","value":"99","unit":"%","as_of":"2024",
                 "qualifiers":{"population_basis":"adults_15plus"},"source_url":"https://id4d.worldbank.org/x","evidence_span":"99%"},
                {"property":"id_coverage_pct","instance_name":"Estonia","value":"95","unit":"%","as_of":"2024",
                 "qualifiers":{"population_basis":"adults_15plus"},"source_url":"https://id4d.worldbank.org/e","evidence_span":"95%"},
                {"property":"id_coverage_pct","instance_name":"Estonia","value":"88","unit":"%","as_of":"2024",
                 "qualifiers":{"population_basis":"adults_15plus"},"source_url":"https://gsma.com/e","evidence_span":"88%"},
            ])
            m = await metrics.compute(conn)
            assert m["total_facts"] == 3
            assert m["trusted_facts"] == 1            # only India's (Estonia's two are in conflict)
            assert m["instances_with_trusted"] == 1   # India
            assert m["open_conflicts"] == 1           # Estonia
            assert 0.0 <= m["groundedness"] <= 1.0
            assert m["groundedness"] == 1.0           # all 3 sources are registry-tier (id4d/gsma authoritative)
    asyncio.run(run())
