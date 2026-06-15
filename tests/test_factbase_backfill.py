import asyncio, aiosqlite
from open_deep_research.factbase import migrations, schema, store, backfill

def test_backfill_records_fetched_and_skipped():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            rs = store.RunSourceStore(conn)
            # 'a' fetches text; 'b' fails (fetcher returns None)
            async def fetcher(url):
                return "India coverage 99% among adults" if url.endswith("/a") else None
            res = await backfill.backfill_run_sources(rs, "t1",
                ["https://x.org/a", "https://y.org/b", "https://x.org/a"],  # dup a
                fetcher)
            assert res == {"fetched": 1, "skipped": 1}
            rows = {r["source_url"]: r for r in await rs.read("t1")}
            assert rows["https://x.org/a"]["capture_status"] == "raw_text"
            assert rows["https://x.org/a"]["text"].startswith("India coverage")
            assert rows["https://y.org/b"]["capture_status"] == "skipped"
    asyncio.run(run())

def test_backfill_skips_urls_already_captured():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            rs = store.RunSourceStore(conn)
            await rs.record("t1", "https://x.org/a", "ALREADY HERE", capture_status="raw_text")
            calls = []
            async def fetcher(url):
                calls.append(url); return "new"
            res = await backfill.backfill_run_sources(rs, "t1", ["https://x.org/a"], fetcher)
            assert calls == []                 # already captured -> not re-fetched
            assert res["fetched"] == 0
    asyncio.run(run())

def test_backfill_caps_url_count():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            rs = store.RunSourceStore(conn)
            n = []
            async def fetcher(url):
                n.append(url); return "t"
            await backfill.backfill_run_sources(rs, "t1",
                [f"https://x.org/{i}" for i in range(50)], fetcher, max_urls=10)
            assert len(n) == 10
    asyncio.run(run())
