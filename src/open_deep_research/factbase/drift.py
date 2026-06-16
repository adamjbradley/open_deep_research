"""Profile-drift detection: has the active profile changed since the last stamped run?

Phase 2 stamps every run with profile_name/profile_version/profile_hash. This compares the
*currently loaded* profile's hash to the latest stamped run for that profile name. A mismatch
(same version, different hash) is the un-versioned-edit erosion signal. Read-only — never
auto-recomputes. Intentionally a DB-aware caller, NOT part of the pure profile.load().
"""
from __future__ import annotations

import aiosqlite

from . import migrations, schema


async def latest_run_profile_hash(db_path: str, profile_name: str) -> str | None:
    """Return the most recent stamped profile_hash for ``profile_name``, or None."""
    from open_deep_research import storage
    async with aiosqlite.connect(db_path) as conn:
        await storage._ensure_schema(conn)          # research_runs base table
        await migrations.apply(conn, schema.STEPS)   # v6 profile columns
        cur = await conn.execute(
            "SELECT profile_hash FROM research_runs "
            "WHERE profile_name=? AND profile_hash IS NOT NULL ORDER BY id DESC LIMIT 1",
            (profile_name,),
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def check_drift(db_path: str, profile_name: str, current_hash: str | None) -> dict:
    """Compare ``current_hash`` to the latest stamped run for ``profile_name``."""
    last = await latest_run_profile_hash(db_path, profile_name)
    drifted = bool(last) and bool(current_hash) and last != current_hash
    return {
        "profile_name": profile_name,
        "current_hash": current_hash,
        "last_run_hash": last,
        "drifted": drifted,
    }
