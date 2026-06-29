from __future__ import annotations
import hashlib
from datetime import datetime, timezone
import aiosqlite
def _hash(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()
class RunSourceStore:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn
    async def record(self, thread_id: str, url: str, text: str | None, *,
                     capture_status: str, reason: str | None = None,
                     title: str | None = None) -> None:
        ch = _hash(text)
        cur = await self._conn.execute(
            "SELECT 1 FROM run_source WHERE thread_id=? AND source_url=? AND content_hash=?",
            (thread_id, url, ch))
        if await cur.fetchone():
            return
        await self._conn.execute(
            "INSERT INTO run_source (thread_id, source_url, capture_status, reason, text, title, content_hash, retrieved_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (thread_id, url, capture_status, reason, text, title, ch,
             datetime.now(timezone.utc).isoformat()))
        await self._conn.commit()

    async def read(self, thread_id: str) -> list[dict]:
        self._conn.row_factory = aiosqlite.Row
        cur = await self._conn.execute(
            "SELECT id, source_url, capture_status, reason, text FROM run_source WHERE thread_id=? AND soft_deleted_at IS NULL",
            (thread_id,))
        return [dict(r) for r in await cur.fetchall()]
