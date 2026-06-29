# tests/test_qualifier_resolution_e2e.py
import asyncio, json, aiosqlite
from open_deep_research.factbase import migrations, schema
from open_deep_research.nodes.qualifiers import resolve_required_qualifiers


def test_property_resolves_by_either_lever(tmp_path, monkeypatch):
    """The required qualifier ends up present (resolved); IF inferred, provenance is stamped.
    Does NOT assert that inference specifically wins (non-deterministic / priority-dependent)."""
    db = str(tmp_path / "f.db")

    async def seed():
        async with aiosqlite.connect(db) as conn:
            await conn.executescript("CREATE TABLE IF NOT EXISTS research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            cur = await conn.execute(
                "INSERT INTO fact (property_name, instance_key, tuple_key, qualifiers_json, value, "
                "admission, lifecycle, run_id, created_at) VALUES "
                "('data_protection_law','EE','tk','{}','true','provisional','current','t1','2026-06-26')")
            fid = cur.lastrowid
            await conn.execute("INSERT INTO evidence (fact_id, quoted_span, retrieved_at) VALUES (?,?,?)",
                               (fid, "the Act is in force since 2019", "2026-06-26"))
            await conn.commit()
            return fid
    fid = asyncio.run(seed())

    async def fake_mc(prompt):
        return '{"value": "in_force", "basis": "stated"}'
    monkeypatch.setattr("open_deep_research.nodes.qualifiers._make_qualifier_model_call",
                        lambda c, cfg: fake_mc)
    cfg = {"configurable": {"thread_id": "t1", "database_path": db,
                            "whole_profile_mode": True, "profile_name": "country_digital_identity"}}
    state = {"prealloc_run_id": "t1", "subject": "Estonia", "qualifier_research_attempted": []}
    asyncio.run(resolve_required_qualifiers(state, cfg))

    async def read():
        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute("SELECT qualifiers_json, qualifier_provenance_json FROM fact WHERE id=?", (fid,))
            return await cur.fetchone()
    q, prov = asyncio.run(read())
    assert json.loads(q).get("stage") == "in_force"          # resolved
    if prov:                                                  # iff inferred, provenance stamped
        assert json.loads(prov).get("stage") == "inferred"
