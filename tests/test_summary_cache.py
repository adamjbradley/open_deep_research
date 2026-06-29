# tests/test_summary_cache.py
import asyncio, aiosqlite
import open_deep_research.utils as utils
from open_deep_research.factbase import schema, migrations, store


def _stub_config(model="claude:haiku"):
    return type("C", (), {"summarize_search_results": True, "max_content_length": 5000,
        "summarization_model": model, "summarization_model_max_tokens": 1000,
        "max_structured_output_retries": 3, "persist_results": False,
        "model_chain": lambda *a, **k: [model]})()


def test_summary_reused_across_runs_skips_model(monkeypatch):
    async def run():
        conn = await aiosqlite.connect(":memory:")
        await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, thread_id TEXT);")
        await conn.commit()
        await migrations.apply(conn, schema.STEPS)
        rs = store.RunSourceStore(conn)
        # content already captured + summarized by a prior run:
        await rs.record("t0", "http://e", "FULL TEXT", capture_status="raw_text")
        from open_deep_research.factbase.store import _hash
        await conn.execute(
            "UPDATE source_content SET summary=?, summary_model=?, summary_prompt_version=? WHERE content_hash=?",
            ("CACHED", "claude:haiku", utils.SUMMARY_PROMPT_VERSION, _hash("FULL TEXT")))
        await conn.commit()

        calls = {"n": 0}
        async def _fake_summarize(model, text):
            calls["n"] += 1
            return "FRESH"
        monkeypatch.setattr(utils, "summarize_webpage", _fake_summarize)
        # route the cache to OUR conn (the resolver passes a conn/db_path — see Step 3)
        summary = await utils._lookup_cached_summary(conn, _hash("FULL TEXT"), "claude:haiku")
        assert summary == "CACHED" and calls["n"] == 0
        # a different model is NOT reused:
        assert await utils._lookup_cached_summary(conn, _hash("FULL TEXT"), "claude:opus") is None
        await conn.close()
    asyncio.run(run())
