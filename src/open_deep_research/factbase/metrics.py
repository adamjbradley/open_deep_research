"""Fact-base health metrics (Feature Spec §4).

ANTI-METRIC: total_facts is reported for visibility but must NEVER be an optimization
target — volume without groundedness is the failure mode.
"""
from __future__ import annotations

import aiosqlite


async def compute(conn: aiosqlite.Connection) -> dict:
    async def scalar(sql: str, params: tuple = ()) -> int:
        cur = await conn.execute(sql, params)
        return (await cur.fetchone())[0]

    total = await scalar("SELECT COUNT(*) FROM fact WHERE soft_deleted_at IS NULL")
    trusted = await scalar("SELECT COUNT(*) FROM fact WHERE admission='trusted' AND soft_deleted_at IS NULL")
    instances_with_trusted = await scalar(
        "SELECT COUNT(DISTINCT instance_key) FROM fact WHERE admission='trusted' AND soft_deleted_at IS NULL")
    open_conflicts = await scalar("SELECT COUNT(*) FROM conflict WHERE status='open'")
    grounded = await scalar(
        "SELECT COUNT(*) FROM fact f JOIN source s ON s.id=f.source_id "
        "WHERE f.soft_deleted_at IS NULL AND s.tier IN ('reputable','authoritative')")
    return {
        "total_facts": total,                      # ANTI-METRIC: never optimize
        "trusted_facts": trusted,
        "provisional_facts": total - trusted,
        "instances_with_trusted": instances_with_trusted,
        "open_conflicts": open_conflicts,
        "groundedness": (grounded / total) if total else 0.0,
    }
