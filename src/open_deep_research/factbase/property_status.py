"""Per-property research status (currently: confirmed-absent records)."""
from __future__ import annotations

import json


class PropertyStatusStore:
    def __init__(self, conn):
        self._conn = conn

    async def record_absent(self, instance_key, property_name, qualifiers, evidence, run_id, as_of):
        await self._conn.execute(
            "INSERT INTO property_status (instance_key, property_name, qualifiers_json, status, "
            "evidence, run_id, as_of) VALUES (?,?,?,?,?,?,?)",
            (
                instance_key,
                property_name,
                json.dumps(qualifiers or {}, sort_keys=True),
                "confirmed_absent",
                evidence,
                run_id,
                as_of,
            ),
        )

    async def absent_properties(self, instance_key) -> set:
        cur = await self._conn.execute(
            "SELECT DISTINCT property_name FROM property_status "
            "WHERE instance_key=? AND status='confirmed_absent'",
            (instance_key,),
        )
        return {r[0] for r in await cur.fetchall()}
