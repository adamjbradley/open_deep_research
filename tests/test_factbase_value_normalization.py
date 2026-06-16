"""Value normalization: semantically-equal fact values dedup/group as one.

Covers the ingest dedup + conflict + backfill + grouped-render integration. Dependency-free
(`:memory:` sqlite, `migrations.apply(conn, schema.STEPS)`), following the repo's
asyncio.run-in-sync-def pattern.
"""
import asyncio
import json

import aiosqlite

from open_deep_research.factbase import (
    conflict,
    entities,
    identity,
    ingest,
    migrations,
    model,
    profile,
    query,
    recompute,
    registry,
    render,
    schema,
)

DI = profile.load("country_digital_identity")
_REP_A = "https://id4d.worldbank.org/a"   # reputable (meets trust bar)
_REP_B = "https://id4d.worldbank.org/b"
_REP_C = "https://id4d.worldbank.org/c"


async def _fresh(conn):
    await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
    await conn.commit()
    await migrations.apply(conn, schema.STEPS)
    return ingest.Ingestor(conn, profile=DI, resolver=entities.CountryResolver(),
                           registry=registry.SourceRegistry.load("di_source_registry"))


def _scheme_rec(value, url):
    return {"property": "foundational_id_scheme", "instance_name": "India", "value": value,
            "as_of": "2024", "qualifiers": {}, "source_url": url, "evidence_span": value}


# -- schema --------------------------------------------------------------------

def test_migration_adds_canonical_columns():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            await migrations.apply(conn, schema.STEPS)  # idempotent re-apply
            cols = [r[1] for r in await (await conn.execute("PRAGMA table_info(fact)")).fetchall()]
            assert "canonical_value" in cols and "canonical_unit" in cols
    asyncio.run(run())


# -- profile alias map ---------------------------------------------------------

def test_property_value_aliases_reverse_lookup():
    pd = DI.property("foundational_id_scheme")
    assert pd.aliases_for("uidai") == "aadhaar"
    assert pd.aliases_for("aadhaar") == "aadhaar"  # canonical maps to itself
    assert pd.aliases_for("something else") is None


# -- ingest: variants collapse, no false conflict ------------------------------

def test_scheme_variants_share_canonical_and_do_not_conflict():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            ing = await _fresh(conn)
            await ing.ingest(run_id=1, records=[
                _scheme_rec("Aadhaar", _REP_A),
                _scheme_rec("Aadhaar Card", _REP_B),
                _scheme_rec("Unique Identity (UID) scheme or Aadhaar", _REP_C),
            ])
            # Three corroborating sources -> three rows, but ONE canonical value.
            assert (await (await conn.execute("SELECT COUNT(*) FROM fact")).fetchone())[0] == 3
            distinct = (await (await conn.execute(
                "SELECT COUNT(DISTINCT canonical_value) FROM fact")).fetchone())[0]
            assert distinct == 1
            assert (await (await conn.execute(
                "SELECT canonical_value FROM fact LIMIT 1")).fetchone())[0] == "aadhaar"
            # ...and they must NOT open a false conflict (the bug being fixed).
            assert (await (await conn.execute(
                "SELECT COUNT(*) FROM conflict WHERE status='open'")).fetchone())[0] == 0
    asyncio.run(run())


def test_within_source_variants_dedup_to_one_row():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            ing = await _fresh(conn)
            await ing.ingest(run_id=1, records=[
                _scheme_rec("Aadhaar", _REP_A),
                _scheme_rec("Aadhaar Card", _REP_A),  # same source, same canonical -> deduped
            ])
            assert (await (await conn.execute("SELECT COUNT(*) FROM fact")).fetchone())[0] == 1
    asyncio.run(run())


def test_percentage_variants_share_canonical():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            ing = await _fresh(conn)
            recs = [
                {"property": "id_coverage_pct", "instance_name": "India", "value": "99%", "unit": "%",
                 "as_of": "2024", "qualifiers": {"population_basis": "adults_15plus"},
                 "source_url": _REP_A, "evidence_span": "99%"},
                {"property": "id_coverage_pct", "instance_name": "India", "value": "99 percent", "unit": None,
                 "as_of": "2024", "qualifiers": {"population_basis": "adults_15plus"},
                 "source_url": _REP_A, "evidence_span": "99 percent"},
            ]
            await ing.ingest(run_id=1, records=recs)
            # same source + same canonical "99" -> one row
            assert (await (await conn.execute("SELECT COUNT(*) FROM fact")).fetchone())[0] == 1
            assert (await (await conn.execute(
                "SELECT canonical_value FROM fact LIMIT 1")).fetchone())[0] == "99"
    asyncio.run(run())


def test_kill_switch_off_keeps_variants_distinct():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            ing = ingest.Ingestor(conn, profile=DI, resolver=entities.CountryResolver(),
                                  registry=registry.SourceRegistry.load("di_source_registry"),
                                  normalize_values=False)
            await ing.ingest(run_id=1, records=[
                _scheme_rec("Aadhaar", _REP_A),
                _scheme_rec("Aadhaar Card", _REP_A),  # off -> raw values differ -> 2 rows
            ])
            assert (await (await conn.execute("SELECT COUNT(*) FROM fact")).fetchone())[0] == 2
    asyncio.run(run())


# -- conflict.detect with canonical_value --------------------------------------

def _cf(fid, value, cval, as_of=2024, bar=True):
    return model.Fact(fid, "t", as_of, value, None, bar, False, canonical_value=cval, canonical_unit=None)


def test_conflict_uses_canonical_value():
    # Same canonical, different raw -> NO conflict.
    assert conflict.detect([_cf(1, "Aadhaar", "aadhaar"), _cf(2, "Aadhaar Card", "aadhaar")]) == []
    # Different canonical -> conflict.
    opens = [i for i in conflict.detect([_cf(1, "Aadhaar", "aadhaar"), _cf(2, "MOSIP", "mosip")])
             if isinstance(i, model.OpenConflict)]
    assert len(opens) == 1


def test_conflict_falls_back_when_canonical_none():
    # Old rows (canonical_value None) fall back to raw canonicalize.
    f1 = model.Fact(1, "t", 2024, "99", "%", True, False)
    f2 = model.Fact(2, "t", 2024, "87", "%", True, False)
    opens = [i for i in conflict.detect([f1, f2]) if isinstance(i, model.OpenConflict)]
    assert len(opens) == 1


# -- backfill (recompute) ------------------------------------------------------

def test_backfill_populates_legacy_rows_idempotently():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            # Legacy rows: canonical_value NULL, raw variant values.
            for v in ("Aadhaar", "Aadhaar Card"):
                await conn.execute(
                    "INSERT INTO fact (tuple_key, instance_key, property_name, value, admission, lifecycle) "
                    "VALUES ('t','IND','foundational_id_scheme',?, 'provisional','current')", (v,))
            await conn.commit()
            n = await recompute.backfill_canonical_values(conn, DI)
            assert n == 2
            distinct = (await (await conn.execute(
                "SELECT COUNT(DISTINCT canonical_value) FROM fact")).fetchone())[0]
            assert distinct == 1  # both -> "aadhaar"
            assert await recompute.backfill_canonical_values(conn, DI) == 0  # idempotent
    asyncio.run(run())


# -- grouped read surface ------------------------------------------------------

def test_grouped_query_and_render_collapse_to_canonical():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            ing = await _fresh(conn)
            await ing.ingest(run_id=1, records=[
                _scheme_rec("Aadhaar", _REP_A),
                _scheme_rec("Aadhaar Card", _REP_B),
            ])
            grouped = await query.FactQuery(conn).show_grouped("IND")
            scheme = [g for g in grouped if g["property_name"] == "foundational_id_scheme"]
            assert len(scheme) == 1
            g = scheme[0]
            assert g["value"] == "aadhaar"
            assert g["source_count"] == 2
            assert set(g["variants"]) == {"Aadhaar", "Aadhaar Card"}
            out = render.render(grouped)
            assert "aadhaar" in out and "2" in out
    asyncio.run(run())
