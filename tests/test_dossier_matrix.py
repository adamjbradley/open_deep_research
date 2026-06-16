from importlib.resources import files

import aiosqlite
import pytest

from open_deep_research import storage as _storage
from open_deep_research.factbase import migrations as _mig, schema as _schema
from open_deep_research.factbase.dossier import run

# country_cbdc profile must be present for this test (it provides the columns).
if not files("open_deep_research.factbase.profiles").joinpath("country_cbdc.yaml").is_file():
    pytest.skip("country_cbdc profile not present", allow_module_level=True)


async def _seed(db_path):
    async with aiosqlite.connect(db_path) as conn:
        await _storage._ensure_schema(conn)
        await _mig.apply(conn, _schema.STEPS)
        await conn.execute(
            "INSERT INTO fact (instance_key, property_name, qualifiers_json, value, "
            "canonical_value, admission, lifecycle) VALUES "
            "('NGA','cbdc_launch_status','{}','launched','launched','trusted','current'),"
            "('IND','cbdc_launch_status','{}','pilot','pilot','provisional','current')")
        await conn.commit()


@pytest.mark.asyncio
async def test_matrix_subcommand_renders_rows(tmp_path):
    db = str(tmp_path / "m.db")
    await _seed(db)
    out = await run(["matrix", "--profile", "country_cbdc", "--format", "md"], db_path=db)
    assert "cbdc_launch_status" in out
    assert "Nigeria" in out and "India" in out
    assert "launched*" in out  # trusted marker
    assert "pilot*" not in out  # provisional value must NOT get the trusted marker


@pytest.mark.asyncio
async def test_matrix_empty_db_reports_no_facts(tmp_path):
    db = str(tmp_path / "empty.db")
    async with aiosqlite.connect(db) as conn:
        await _storage._ensure_schema(conn)
        await _mig.apply(conn, _schema.STEPS)
    out = await run(["matrix", "--profile", "country_cbdc"], db_path=db)
    assert "No facts found" in out
