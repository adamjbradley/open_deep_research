import asyncio
import open_deep_research.deep_researcher as dr


def test_gap_round_narrows_target_properties(monkeypatch, tmp_path):
    # Force the "missing" branch deterministically by stubbing coverage.
    monkeypatch.setattr(dr, "_target_property_coverage",
                        lambda grouped, targets: ({t: False for t in targets}, {}))
    # Resolve a subject + a fake instance key so the DB branch is entered, then short-circuit the query.
    import open_deep_research.factbase.entities as ent
    monkeypatch.setattr(ent.CountryResolver, "resolve", lambda self, s: "BRA")
    async def fake_grouped(self, key): return []
    import open_deep_research.factbase.query as q
    monkeypatch.setattr(q.FactQuery, "show_grouped", fake_grouped)

    state = {"target_properties": ["legal_basis", "id_coverage_pct"], "subject": "Brazil",
             "fact_rounds_used": 0}
    cmd = asyncio.run(dr.assess_sufficiency(state, {"configurable": {"max_fact_rounds": 3,
                                                                     "database_path": str(tmp_path/'x.db')}}))
    assert cmd.goto == "write_research_brief"
    assert cmd.update["target_properties"] == ["legal_basis", "id_coverage_pct"]


def test_db_error_loops_instead_of_finishing(monkeypatch, tmp_path):
    import open_deep_research.factbase.entities as ent
    monkeypatch.setattr(ent.CountryResolver, "resolve", lambda self, s: "BRA")
    import open_deep_research.factbase.query as q
    async def boom(self, key): raise RuntimeError("db locked")
    monkeypatch.setattr(q.FactQuery, "show_grouped", boom)
    state = {"target_properties": ["legal_basis"], "subject": "Brazil", "fact_rounds_used": 0}
    cmd = asyncio.run(dr.assess_sufficiency(state, {"configurable": {"max_fact_rounds": 3,
                                                                     "database_path": str(tmp_path/'x.db')}}))
    assert cmd.goto == "write_research_brief"   # loops on error, does NOT finalize thin
