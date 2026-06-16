import asyncio

import aiosqlite

from open_deep_research import storage as _storage
from open_deep_research.factbase import migrations as _mig, schema as _schema
from open_deep_research.factbase.batch_ledger import BatchLedger, batch_id_for


def test_batch_tables_exist_after_migration(tmp_path):
    async def _run():
        db = str(tmp_path / "b.db")
        async with aiosqlite.connect(db) as conn:
            await _storage._ensure_schema(conn)
            await _mig.apply(conn, _schema.STEPS)
            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('batch_run','batch_item')")
            return sorted(r[0] for r in await cur.fetchall())
    assert asyncio.run(_run()) == ["batch_item", "batch_run"]


def test_batch_id_is_deterministic():
    a = batch_id_for("country_cbdc", "Nigeria, India")
    b = batch_id_for("country_cbdc", "Nigeria, India")
    c = batch_id_for("country_cbdc", "Nigeria, Bahamas")
    assert a == b and a != c


def test_batch_id_is_order_insensitive():
    # Same country set in any order -> same batch (re-run reattaches regardless of order).
    assert (batch_id_for("country_cbdc", "India, Nigeria")
            == batch_id_for("country_cbdc", "Nigeria, India"))


def test_ledger_resume_skips_done(tmp_path):
    import asyncio

    async def _run():
        db = str(tmp_path / "l.db")
        async with aiosqlite.connect(db) as conn:
            await _storage._ensure_schema(conn)
            await _mig.apply(conn, _schema.STEPS)
            led = BatchLedger(conn, "bid1", profile_name="p", profile_hash="h", list_spec="s")
            await led.ensure_run()
            await led.upsert_item("NGA", "Nigeria", status="pending")
            await led.upsert_item("IND", "India", status="pending")
            await led.mark("NGA", status="done", run_id="7")
            pending = await led.pending_items()
            keys_after_done = [i["instance_key"] for i in pending]
            await led.mark("IND", status="failed", error="boom")
            retry = await led.pending_items(include_failed=True)
            keys_retry = [i["instance_key"] for i in retry]
            summ = await led.summary()
            return keys_after_done, keys_retry, summ
    keys_after_done, keys_retry, summ = asyncio.run(_run())
    assert keys_after_done == ["IND"]      # NGA done -> skipped
    assert keys_retry == ["IND"]           # failed is retryable
    assert summ.get("done") == 1 and summ.get("failed") == 1
