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


def _statements(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]


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
            # Open an explicit transaction so DDL (CREATE TABLE, ...) is also
            # covered by the rollback. sqlite3's implicit transactions only
            # begin before DML, so without this a failing multi-statement step
            # would leak already-applied DDL.
            await conn.execute("BEGIN")
            for stmt in _statements(sql):
                try:
                    await conn.execute(stmt)
                except aiosqlite.OperationalError as exc:
                    # Make `ALTER TABLE ... ADD COLUMN` idempotent: SQLite has no
                    # `ADD COLUMN IF NOT EXISTS`, so a column already present in
                    # the live schema (e.g. research_runs.status from
                    # storage._SCHEMA) raises "duplicate column name". Skip those
                    # and let the step add the columns that are genuinely new.
                    if "duplicate column name" in str(exc).lower():
                        continue
                    raise
            await conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) "
                "VALUES (?, datetime('now'))",
                (version,),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
