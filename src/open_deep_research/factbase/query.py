"""Read-only fact-base queries for the dossier surface."""
from __future__ import annotations
import json
import aiosqlite


class FactQuery:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    async def _rows(self, where: str, params: tuple) -> list[dict]:
        self._conn.row_factory = aiosqlite.Row
        sql = (
            "SELECT f.id, f.instance_key, f.property_name, f.qualifiers_json, f.as_of, f.value, "
            "f.unit, f.admission, f.lifecycle, s.url_or_domain AS source_url, s.tier AS source_tier, "
            "EXISTS (SELECT 1 FROM conflict_member cm JOIN conflict c ON c.id=cm.conflict_id "
            "        WHERE cm.fact_id=f.id AND c.status='open') AS in_conflict "
            "FROM fact f LEFT JOIN source s ON s.id=f.source_id "
            f"WHERE f.soft_deleted_at IS NULL AND {where} "
            "ORDER BY f.property_name, f.as_of"
        )
        cur = await self._conn.execute(sql, params)
        out = []
        for r in await cur.fetchall():
            d = dict(r)
            d["qualifiers"] = json.loads(d.get("qualifiers_json") or "{}")
            d["in_conflict"] = bool(d["in_conflict"])
            out.append(d)
        return out

    async def show(self, instance_key: str) -> list[dict]:
        return await self._rows("f.instance_key = ?", (instance_key,))

    async def compare(self, property_name: str) -> list[dict]:
        return await self._rows("f.property_name = ?", (property_name,))
