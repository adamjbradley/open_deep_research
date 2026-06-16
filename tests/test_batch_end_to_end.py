import asyncio
from importlib.resources import files

import aiosqlite
import pytest

from open_deep_research import storage as _storage
from open_deep_research.factbase import migrations as _mig, schema as _schema
from open_deep_research.factbase.batch import BatchRunner

if not files("open_deep_research.factbase.profiles").joinpath("country_cbdc.yaml").is_file():
    pytest.skip("country_cbdc profile not present", allow_module_level=True)


def test_two_country_batch_persists_ledger_and_matrix(tmp_path):
    db = str(tmp_path / "e2e.db")

    async def fake_run_one(country_name, instance_key, *, profile_name, db_path):
        # Simulate the graph writing one fact per country.
        async with aiosqlite.connect(db_path) as conn:
            await _storage._ensure_schema(conn)
            await _mig.apply(conn, _schema.STEPS)
            await conn.execute(
                "INSERT INTO fact (instance_key, property_name, qualifiers_json, value, "
                "canonical_value, admission, lifecycle) VALUES (?,?,?,?,?,?,?)",
                (instance_key, "cbdc_launch_status", "{}", "launched", "launched",
                 "provisional", "current"))
            await conn.commit()
        return "rid-" + instance_key

    runner = BatchRunner(profile_name="country_cbdc", db_path=db, concurrency=2,
                         run_one=fake_run_one)
    res = asyncio.run(runner.run(["Nigeria", "Bahamas"]))
    assert res["summary"]["done"] == 2

    # matrix renders both countries via the dossier CLI
    from open_deep_research.factbase.dossier import run
    out = asyncio.run(run(["matrix", "--profile", "country_cbdc", "--format", "md"], db_path=db))
    assert "Nigeria" in out and "Bahamas" in out


def test_batch_dry_run_reports_resolution(tmp_path):
    from open_deep_research.factbase.dossier import run
    db = str(tmp_path / "dry.db")
    out = asyncio.run(run(
        ["batch", "--profile", "country_cbdc", "--countries", "Nigeria,Atlantis", "--dry-run"],
        db_path=db))
    assert "NGA" in out                  # Nigeria resolves
    assert "UNRESOLVED" in out           # Atlantis reported, not silently dropped
