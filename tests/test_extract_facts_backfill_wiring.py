import asyncio
from open_deep_research import deep_researcher as dr

def test_extract_facts_harvests_urls_and_backfills(monkeypatch, tmp_path):
    db = str(tmp_path / "f.db")
    async def fake_fetch(url, **kw):
        return "India: coverage was 99% among adults in 2024."
    monkeypatch.setattr(dr, "_fact_fetch_text", fake_fetch, raising=False)
    async def fake_model_call_factory(configurable, config):
        async def _call(text, prof):
            return [{"property":"id_coverage_pct","instance_name":"India","value":"99","unit":"%",
                     "as_of":"2024","qualifiers":{"population_basis":"adults_15plus"},
                     "evidence_span":"coverage was 99% among adults"}]
        return _call
    monkeypatch.setattr(dr, "_make_fact_model_call", fake_model_call_factory, raising=False)
    from langchain_core.runnables import RunnableConfig
    from open_deep_research import storage
    rid = asyncio.run(storage.preallocate_run(db, "t-bf"))
    state = {"final_report": "See https://id4d.worldbank.org/india for details.",
             "raw_notes": [], "prealloc_run_id": rid}
    cfg = RunnableConfig(configurable={"persist_results": True, "thread_id": "t-bf", "database_path": db})
    asyncio.run(dr.extract_facts(state, cfg))
    import aiosqlite
    async def check():
        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM fact")
            return (await cur.fetchone())[0]
    assert asyncio.run(check()) >= 1
