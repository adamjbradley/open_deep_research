import asyncio

import aiosqlite

from open_deep_research import storage as _storage
from open_deep_research.factbase import migrations as _mig, schema as _schema


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
