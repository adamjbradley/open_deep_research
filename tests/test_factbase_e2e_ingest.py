"""End-to-end (no live LLM, no full graph) smoke test for the fact base.

Captured ``run_source`` rows flow through extraction (with a STUB model_call)
and ingestion. Two trust-bar sources (id4d, gsma — both 'authoritative' in the
registry) give *different* coverage values for India under the SAME identity
qualifiers/as_of, so we expect 2 facts + 1 open conflict, neither promoted.

The stub's ``evidence_span`` must be a verbatim (whitespace-normalized)
substring of each source's stored ``text`` or ``extractor.extract`` will
(correctly) drop the record.
"""
import asyncio

import aiosqlite

from open_deep_research.factbase import (
    entities,
    extractor,
    ingest,
    migrations,
    profile,
    registry,
    schema,
    store,
)

DI = profile.load("country_digital_identity")


def _stub(value):
    async def _call(text, prof):
        return [{
            "property": "id_coverage_pct",
            "instance_name": "India",
            "value": value,
            "unit": "%",
            "as_of": "2024",
            "qualifiers": {"population_basis": "adults_15plus"},
            "evidence_span": f"coverage was {value}% among adults",
        }]
    return _call


def test_run_sources_to_facts_end_to_end():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript(
                "CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)

            rs = store.RunSourceStore(conn)
            # Source texts CONTAIN the spans the stub will emit (verbatim).
            await rs.record(
                "t1", "https://id4d.worldbank.org/x",
                "India: coverage was 99% among adults in 2024.",
                capture_status="raw_text")
            await rs.record(
                "t1", "https://gsma.com/y",
                "India: coverage was 87% among adults in 2024.",
                capture_status="raw_text")

            all_records = []
            for s in await rs.read("t1"):
                value = "99" if "id4d" in s["source_url"] else "87"
                recs = await extractor.extract(s["text"], DI, _stub(value))
                for r in recs:
                    r["source_url"] = s["source_url"]
                all_records += recs

            # Both spans verified against their own source text.
            assert len(all_records) == 2

            ingestor = ingest.Ingestor(
                conn,
                profile=DI,
                resolver=entities.CountryResolver(),
                registry=registry.SourceRegistry.load("di_source_registry"),
            )
            await ingestor.ingest(run_id=1, records=all_records)

            cur = await conn.execute("SELECT COUNT(*) FROM fact")
            assert (await cur.fetchone())[0] == 2
            cur = await conn.execute(
                "SELECT COUNT(*) FROM conflict WHERE status='open'")
            assert (await cur.fetchone())[0] == 1
            cur = await conn.execute(
                "SELECT COUNT(*) FROM fact WHERE admission='trusted'")
            assert (await cur.fetchone())[0] == 0

    asyncio.run(run())
