"""Versioned SQLite migration framework for the fact base.

Exposes a single async :func:`apply` that runs a list of ``(version, sql)``
steps against an ``aiosqlite`` connection. Applied versions are tracked in a
``schema_migrations`` table so re-applying the same steps is idempotent. Each
step and its tracking insert run transactionally (rollback on failure).
"""
from __future__ import annotations

import aiosqlite

_TRACKING = (
    "CREATE TABLE IF NOT EXISTS schema_migrations "
    "(version INTEGER PRIMARY KEY, applied_at TEXT)"
)


async def _applied_versions(conn: aiosqlite.Connection) -> set[int]:
    cur = await conn.execute("SELECT version FROM schema_migrations")
    return {row[0] for row in await cur.fetchall()}


async def apply(conn: aiosqlite.Connection, steps: list[tuple[int, str]]) -> None:
    """Apply pending migration ``steps`` in ascending version order.

    Creates the ``schema_migrations`` tracking table if absent, then runs each
    step whose version is not already recorded, recording it on success. Each
    step plus its tracking insert is committed atomically; a failure rolls the
    step back and re-raises.
    """
    await conn.execute(_TRACKING)
    await conn.commit()
    done = await _applied_versions(conn)
    for version, sql in sorted(steps, key=lambda s: s[0]):
        if version in done:
            continue
        try:
            await conn.executescript(sql)
            await conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) "
                "VALUES (?, datetime('now'))",
                (version,),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
