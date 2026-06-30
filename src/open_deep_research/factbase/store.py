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
        # Dedup unique content into source_content (raw, non-empty only) so the
        # text + its summary are stored once across runs. Idempotent.
        if text:
            await self._conn.execute(
                "INSERT OR IGNORE INTO source_content "
                "(content_hash, source_url, title, text, first_seen_at) VALUES (?,?,?,?,?)",
                (ch, url, title, text, datetime.now(timezone.utc).isoformat()))
        cur = await self._conn.execute(
            "SELECT 1 FROM run_source WHERE thread_id=? AND source_url=? AND content_hash=?",
            (thread_id, url, ch))
        if await cur.fetchone():
            await self._conn.commit()
            return
        await self._conn.execute(
            "INSERT INTO run_source (thread_id, source_url, capture_status, reason, text, title, content_hash, retrieved_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (thread_id, url, capture_status, reason, None, title, ch,
             datetime.now(timezone.utc).isoformat()))
        await self._conn.commit()

    async def read(self, thread_id: str) -> list[dict]:
        self._conn.row_factory = aiosqlite.Row
        cur = await self._conn.execute(
            "SELECT rs.id, rs.source_url, rs.capture_status, rs.reason, rs.title, "
            "       COALESCE(rs.text, sc.text) AS text "
            "FROM run_source rs "
            "LEFT JOIN source_content sc ON sc.content_hash = rs.content_hash "
            "WHERE rs.thread_id=? AND rs.soft_deleted_at IS NULL",
            (thread_id,))
        return [dict(r) for r in await cur.fetchall()]
