import asyncio, aiosqlite
from open_deep_research.factbase import migrations, schema, ingest, profile, entities, registry, dossier
DI = profile.load("country_digital_identity")
def _seed_db(db_path):
    async def run():
        async with aiosqlite.connect(db_path) as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            recs = [
                {"property":"id_coverage_pct","instance_name":"India","value":"99","unit":"%","as_of":"2024",
                 "qualifiers":{"population_basis":"adults_15plus"},"source_url":"https://id4d.worldbank.org/x","evidence_span":"99%"},
                {"property":"id_coverage_pct","instance_name":"India","value":"87","unit":"%","as_of":"2024",
                 "qualifiers":{"population_basis":"adults_15plus"},"source_url":"https://gsma.com/y","evidence_span":"87%"},
            ]
            await ingest.Ingestor(conn, profile=DI, resolver=entities.CountryResolver(),
                                  registry=registry.SourceRegistry.load("di_source_registry")).ingest(run_id=1, records=recs)
    asyncio.run(run())
def test_show_renders_conflict(tmp_path):
    db = str(tmp_path / "f.db"); _seed_db(db)
    out = asyncio.run(dossier.run(["show", "India", "--format", "text"], db_path=db))
    assert "⚠" in out and "99" in out and "87" in out
def test_show_unknown_country(tmp_path):
    db = str(tmp_path / "f.db"); _seed_db(db)
    out = asyncio.run(dossier.run(["show", "Atlantis"], db_path=db))
    assert "unknown country" in out.lower() or "could not resolve" in out.lower()
def test_compare_csv(tmp_path):
    db = str(tmp_path / "f.db"); _seed_db(db)
    out = asyncio.run(dossier.run(["compare", "id_coverage_pct", "--format", "csv"], db_path=db))
    assert out.splitlines()[0].startswith("instance_key,")
