import asyncio

import aiosqlite

from open_deep_research.factbase import migrations, schema
from open_deep_research.factbase.property_status import PropertyStatusStore


def test_record_and_read_absent():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript(
                "CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);"
            )
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            store = PropertyStatusStore(conn)
            await store.record_absent(
                "EST",
                "biometric_capture",
                {},
                "searched 5 sources; none state biometrics",
                1,
                None,
            )
            await conn.commit()
            assert await store.absent_properties("EST") == {"biometric_capture"}
            assert await store.absent_properties("IND") == set()

    asyncio.run(run())
