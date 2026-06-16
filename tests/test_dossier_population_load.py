import asyncio
import sqlite3

from open_deep_research.factbase.dossier import run


def test_population_load_subcommand(tmp_path, monkeypatch):
    db = str(tmp_path / "pl.db")
    # stub the data read so the CLI test needs no network / real population.yaml
    from open_deep_research.factbase import population_loader as pl
    monkeypatch.setattr(pl, "_load_data",
                        lambda: {"NGA": {"value": 223804632, "year": 2023}})
    out = asyncio.run(run(["population-load"], db_path=db))
    assert "loaded 1" in out and "trusted" in out

    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM fact WHERE property_name='population'").fetchone()[0]
    conn.close()
    assert n == 1
