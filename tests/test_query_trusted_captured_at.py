import asyncio, aiosqlite
from open_deep_research.factbase import schema, migrations, query


async def _seed(conn):
    await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, thread_id TEXT);")
    await conn.commit()
    await migrations.apply(conn, schema.STEPS)
    # two facts for the same (instance, property, canonical value): one trusted (older),
    # one provisional (newer). trusted_captured_at must be the TRUSTED row's created_at.
    await conn.execute("INSERT INTO fact (instance_key, property_name, tuple_key, value, canonical_value, "
                       "admission, lifecycle, as_of, created_at) "
                       "VALUES ('EST','legal_basis','EST|legal_basis','Act X','Act X','trusted','current',2020,'2026-01-01T00:00:00Z')")
    await conn.execute("INSERT INTO fact (instance_key, property_name, tuple_key, value, canonical_value, "
                       "admission, lifecycle, as_of, created_at) "
                       "VALUES ('EST','legal_basis','EST|legal_basis','Act X','Act X','provisional','current',2020,'2026-06-01T00:00:00Z')")
    await conn.commit()


def test_trusted_captured_at_is_max_over_trusted_rows():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            grouped = await query.FactQuery(conn).show_grouped("EST")
            row = next(g for g in grouped if g["property_name"] == "legal_basis")
            # admission is "trusted" (any-trusted), and trusted_captured_at is the TRUSTED row's ts
            assert row["admission"] == "trusted"
            assert row["trusted_captured_at"] == "2026-01-01T00:00:00Z"
    asyncio.run(run())


def test_trusted_captured_at_none_when_no_trusted_row():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, thread_id TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            await conn.execute("INSERT INTO fact (instance_key, property_name, tuple_key, value, canonical_value, "
                               "admission, lifecycle, as_of, created_at) "
                               "VALUES ('EST','x','EST|x','v','v','provisional','current',2020,'2026-06-01T00:00:00Z')")
            await conn.commit()
            grouped = await query.FactQuery(conn).show_grouped("EST")
            assert grouped[0]["trusted_captured_at"] is None
    asyncio.run(run())
