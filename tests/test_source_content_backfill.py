import asyncio, aiosqlite
from open_deep_research.factbase import schema, migrations


def test_backfill_dedups_and_excludes_empty_and_is_idempotent():
    async def run():
        conn = await aiosqlite.connect(":memory:")
        await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, thread_id TEXT);")
        await conn.commit()
        # apply through v13 only, seed legacy rows, then apply v14
        await migrations.apply(conn, [s for s in schema.STEPS if s[0] <= 13])
        await conn.execute("INSERT INTO run_source (thread_id, source_url, capture_status, text, content_hash) "
                           "VALUES ('t1','http://a','raw_text','BODY','hA')")
        await conn.execute("INSERT INTO run_source (thread_id, source_url, capture_status, text, content_hash) "
                           "VALUES ('t2','http://a','raw_text','BODY','hA')")  # dup content
        await conn.execute("INSERT INTO run_source (thread_id, source_url, capture_status, text, content_hash) "
                           "VALUES ('t3','http://b',  'summarized', NULL, ?)",
                           (__import__('hashlib').sha256(b'').hexdigest(),))      # empty capture
        await conn.commit()
        await migrations.apply(conn, schema.STEPS)          # runs v14
        sc = await (await conn.execute("SELECT count(*) FROM source_content")).fetchone()
        assert sc[0] == 1                                    # 'BODY' deduped; empty excluded
        txt = await (await conn.execute("SELECT text FROM source_content WHERE content_hash='hA'")).fetchone()
        assert txt[0] == "BODY"
        nulled = await (await conn.execute("SELECT count(*) FROM run_source WHERE text IS NOT NULL")).fetchone()
        assert nulled[0] == 0                                # run_source.text nulled
        await migrations.apply(conn, schema.STEPS)           # idempotent (v14 already applied)
        await conn.close()
    asyncio.run(run())
