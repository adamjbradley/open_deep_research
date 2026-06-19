"""SQLite ledger for resumable batch runs: one batch_run, many batch_item rows.

batch_id is derived from (profile, normalized list spec) so a re-run reattaches and
skips items already 'done'. Timestamps use the SQLite ``datetime('now')`` default so
the ledger stays deterministic from the caller's side. Each write commits immediately
(one flush per status transition): deliberate, so a crash mid-batch leaves a durable,
resumable ledger rather than losing in-flight progress.
"""
from __future__ import annotations

import hashlib

import aiosqlite

_STATUSES = frozenset({"pending", "running", "done", "failed"})


def _check_status(status: str) -> None:
    if status not in _STATUSES:
        raise ValueError(f"invalid status {status!r}; must be one of {sorted(_STATUSES)}")


def batch_id_for(profile_name: str, list_spec: str) -> str:
    """Deterministic batch id from a profile + normalized list spec (order-insensitive)."""
    norm = ",".join(sorted(
        p.strip().lower()
        for p in (list_spec or "").replace("\n", ",").split(",")
        if p.strip()))
    raw = f"{profile_name}|{norm}"
    return "b_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class BatchLedger:
    """Read/write access to one batch's ledger rows (resume-aware)."""

    def __init__(self, conn: aiosqlite.Connection, batch_id: str, *,
                 profile_name: str, profile_hash: str, list_spec: str):
        self._conn = conn
        self._conn.row_factory = aiosqlite.Row  # set once; reads return mapping rows
        self.batch_id = batch_id
        self._meta = (profile_name, profile_hash, list_spec)

    async def ensure_run(self) -> None:
        """Insert the batch_run row if absent (idempotent)."""
        await self._conn.execute(
            "INSERT OR IGNORE INTO batch_run "
            "(batch_id, profile_name, profile_hash, list_spec, created_at) "
            "VALUES (?,?,?,?, datetime('now'))",
            (self.batch_id, *self._meta))
        await self._conn.commit()

    async def upsert_item(self, instance_key: str, country_name: str, *,
                          status: str = "pending") -> None:
        """Insert a batch_item if absent; leaves an existing item's status untouched."""
        _check_status(status)
        await self._conn.execute(
            "INSERT INTO batch_item "
            "(batch_id, instance_key, country_name, status, updated_at) "
            "VALUES (?,?,?,?, datetime('now')) "
            "ON CONFLICT(batch_id, instance_key) DO NOTHING",
            (self.batch_id, instance_key, country_name, status))
        await self._conn.commit()

    async def mark(self, instance_key: str, *, status: str, run_id: str | None = None,
                   error: str | None = None) -> None:
        """Update an item's status (+ optional run_id/error). 'failed' increments attempt_count."""
        _check_status(status)
        inc = ", attempt_count = attempt_count + 1" if status == "failed" else ""
        await self._conn.execute(
            f"UPDATE batch_item SET status=?, run_id=?, error=?, updated_at=datetime('now'){inc} "
            "WHERE batch_id=? AND instance_key=?",
            (status, run_id, error, self.batch_id, instance_key))
        await self._conn.commit()

    async def pending_items(self, *, include_failed: bool = True) -> list[dict]:
        """Items still needing work: pending/running (+ failed when include_failed)."""
        statuses = ["pending", "running"] + (["failed"] if include_failed else [])
        ph = ",".join("?" for _ in statuses)
        cur = await self._conn.execute(
            f"SELECT instance_key, country_name, status FROM batch_item "
            f"WHERE batch_id=? AND status IN ({ph}) ORDER BY instance_key",
            (self.batch_id, *statuses))
        return [dict(r) for r in await cur.fetchall()]

    async def summary(self) -> dict:
        """Map of status -> count for this batch."""
        cur = await self._conn.execute(
            "SELECT status, COUNT(*) n FROM batch_item WHERE batch_id=? GROUP BY status",
            (self.batch_id,))
        return {r["status"]: r["n"] for r in await cur.fetchall()}
