# tests/test_resolve_required_qualifiers_node.py
import asyncio, json, aiosqlite
from open_deep_research.factbase import migrations, schema
from open_deep_research.nodes.qualifiers import resolve_required_qualifiers


async def _seed_fact(db, *, qualifiers, run_id="t1"):
    async with aiosqlite.connect(db) as conn:
        await conn.executescript("CREATE TABLE IF NOT EXISTS research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
        await conn.commit()
        await migrations.apply(conn, schema.STEPS)
        cur = await conn.execute(
            "INSERT INTO fact (property_name, instance_key, tuple_key, qualifiers_json, value, "
            "admission, lifecycle, run_id, created_at) VALUES "
            "('data_protection_law','EE','tk',?, 'true','provisional','current',?, '2026-06-26')",
            (json.dumps(qualifiers), run_id))
        fid = cur.lastrowid
        await conn.execute(
            "INSERT INTO evidence (fact_id, quoted_span, retrieved_at) VALUES (?,?,?)",
            (fid, "the Personal Data Protection Act is in force since 2019", "2026-06-26"))
        await conn.commit()
        return fid


def test_resolver_fills_stated_qualifier(tmp_path, monkeypatch):
    db = str(tmp_path / "f.db")
    fid = asyncio.run(_seed_fact(db, qualifiers={}))

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
    assert json.loads(q).get("stage") == "in_force"
    assert prov is None  # stated -> no inferred-provenance marker


def test_resolver_defers_inference_until_research_attempted(tmp_path, monkeypatch):
    db = str(tmp_path / "f.db")
    fid = asyncio.run(_seed_fact(db, qualifiers={}))

    async def fake_mc(prompt):
        # model would infer, but allow_inference must be False -> resolve_qualifier returns None
        return '{"value": "in_force", "basis": "inferred"}'
    monkeypatch.setattr("open_deep_research.nodes.qualifiers._make_qualifier_model_call",
                        lambda c, cfg: fake_mc)
    cfg = {"configurable": {"thread_id": "t1", "database_path": db,
                            "whole_profile_mode": True, "profile_name": "country_digital_identity"}}
    state = {"prealloc_run_id": "t1", "subject": "Estonia", "qualifier_research_attempted": []}
    asyncio.run(resolve_required_qualifiers(state, cfg))

    async def read():
        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute("SELECT qualifiers_json FROM fact WHERE id=?", (fid,))
            return await cur.fetchone()
    (q,) = asyncio.run(read())
    assert "stage" not in json.loads(q)  # deferred, not inferred


def test_resolver_infers_when_research_attempted(tmp_path, monkeypatch):
    db = str(tmp_path / "f.db")
    fid = asyncio.run(_seed_fact(db, qualifiers={}))

    async def fake_mc(prompt):
        return '{"value": "in_force", "basis": "inferred"}'
    monkeypatch.setattr("open_deep_research.nodes.qualifiers._make_qualifier_model_call",
                        lambda c, cfg: fake_mc)
    cfg = {"configurable": {"thread_id": "t1", "database_path": db,
                            "whole_profile_mode": True, "profile_name": "country_digital_identity"}}
    state = {"prealloc_run_id": "t1", "subject": "Estonia",
             "qualifier_research_attempted": ["data_protection_law::stage"]}
    asyncio.run(resolve_required_qualifiers(state, cfg))

    async def read():
        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute("SELECT qualifiers_json, qualifier_provenance_json FROM fact WHERE id=?", (fid,))
            return await cur.fetchone()
    q, prov = asyncio.run(read())
    assert json.loads(q).get("stage") == "in_force"
    assert json.loads(prov).get("stage") == "inferred"  # inferred -> marked
