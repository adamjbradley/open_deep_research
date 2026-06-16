import asyncio
import sqlite3

from open_deep_research.factbase.population_loader import load_population


def test_load_population_ingests_trusted_facts(tmp_path):
    db = str(tmp_path / "pop.db")
    data = {"NGA": {"value": 223804632, "year": 2023},
            "BHS": {"value": 412623, "year": 2023}}
    result = asyncio.run(load_population(db, data=data))

    assert result["loaded"] == 2
    assert result["instances"] == 2
    assert result["trusted"] >= 1          # World Bank authoritative -> promoted

    conn = sqlite3.connect(db)
    rows = dict(conn.execute(
        "SELECT instance_key, value FROM fact WHERE property_name='population'").fetchall())
    assert rows["NGA"] == "223804632"
    trusted = conn.execute(
        "SELECT COUNT(*) FROM fact WHERE property_name='population' AND admission='trusted'"
    ).fetchone()[0]
    assert trusted >= 1
    src = conn.execute(
        "SELECT s.url_or_domain FROM fact f JOIN source s ON s.id=f.source_id "
        "WHERE f.property_name='population' LIMIT 1").fetchone()[0]
    assert "worldbank.org" in src
    conn.close()


def test_load_population_reports_unresolved(tmp_path):
    db = str(tmp_path / "pop2.db")
    data = {"NGA": {"value": 1, "year": 2023}, "ZZZ": {"value": 2, "year": 2023}}
    result = asyncio.run(load_population(db, data=data))
    assert "ZZZ" in result["skipped"]      # not a resolvable ISO country -> reported
    assert result["loaded"] == 1
