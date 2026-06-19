"""Local SQLite persistence for research runs, grouped and accumulated by subject.

Every completed `deep_researcher` run is written to a local SQLite file (via
``aiosqlite``) by the ``persist_research`` graph node. Runs are grouped by a
canonical *subject*: each run is stored individually in ``research_runs`` (full
history), and merged into that subject's accumulated dossier in ``subjects`` so
later questions about a different aspect of the same subject add to -- rather than
replace -- what is already known. No external service is required (SQLite is stdlib).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite

DEFAULT_DB_PATH = "research_results.db"

_URL_RE = re.compile(r"https?://[^\s)\]\}<>\"']+")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subjects (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    slug           TEXT UNIQUE,
    name           TEXT,
    current_report TEXT,   -- accumulated dossier (merged across runs)
    sources        TEXT,   -- JSON array of all source URLs seen for this subject
    run_count      INTEGER DEFAULT 0,
    created_at     TEXT,
    updated_at     TEXT
);
CREATE TABLE IF NOT EXISTS research_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id     INTEGER REFERENCES subjects(id),
    thread_id      TEXT,
    topic          TEXT,
    research_brief TEXT,
    final_report   TEXT,   -- this run's report only
    sources        TEXT,   -- JSON array (this run)
    raw_notes      TEXT,   -- JSON array (this run)
    config         TEXT,   -- JSON object of the config used
    status         TEXT,   -- 'completed' | 'error'
    error          TEXT,
    created_at     TEXT
);
CREATE TABLE IF NOT EXISTS dossier_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id  INTEGER REFERENCES subjects(id),
    run_id      INTEGER REFERENCES research_runs(id),
    dossier     TEXT,   -- snapshot of the accumulated dossier at this update
    sources     TEXT,   -- JSON array snapshot
    created_at  TEXT    -- ISO-8601 UTC; ordered timeline of how the subject evolved
);
"""


def get_db_path(config: Optional[dict] = None) -> str:
    """Resolve the SQLite file path.

    Precedence: ``Configuration.database_path`` (via config) -> env
    ``RESEARCH_DB_PATH`` -> default ``research_results.db`` (relative to cwd,
    which is the project root under ``langgraph dev``).
    """
    configurable = (config or {}).get("configurable", {}) if config else {}
    return (
        configurable.get("database_path")
        or os.getenv("RESEARCH_DB_PATH")
        or DEFAULT_DB_PATH
    )


def slugify(name: str) -> str:
    """Normalize a subject name to a stable matching key (case/punctuation-insensitive)."""
    return _SLUG_STRIP_RE.sub("-", (name or "").strip().lower()).strip("-") or "unknown"


def extract_sources(*texts: Any) -> list[str]:
    """Extract de-duplicated source URLs (order-preserving) from text blobs."""
    seen: dict[str, None] = {}
    for text in texts:
        if not text:
            continue
        for match in _URL_RE.findall(str(text)):
            url = match.rstrip(".,;")
            if url not in seen:
                seen[url] = None
    return list(seen.keys())


async def _ensure_schema(conn: aiosqlite.Connection) -> None:
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.executescript(_SCHEMA)


async def get_subject_names(db_path: str) -> list[str]:
    """Return the canonical names of all subjects already in the knowledge base."""
    async with aiosqlite.connect(db_path) as conn:
        await _ensure_schema(conn)
        cursor = await conn.execute("SELECT name FROM subjects ORDER BY name")
        return [row[0] for row in await cursor.fetchall()]


async def get_subject_by_slug(db_path: str, slug: str) -> Optional[dict]:
    """Return the existing subject row (id, name, current_report, sources) or None."""
    async with aiosqlite.connect(db_path) as conn:
        await _ensure_schema(conn)
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT id, name, current_report, sources, updated_at FROM subjects WHERE slug = ?",
            (slug,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "current_report": row["current_report"],
            "sources": json.loads(row["sources"] or "[]"),
            "updated_at": row["updated_at"],
        }


async def log_research_run(
    db_path: str, slug: str, run: dict, run_id: Optional[int] = None
) -> Optional[int]:
    """Insert a research_runs row under an existing subject without changing the
    dossier (used when a question was answered from the cache). Returns the run id.

    If ``run_id`` is given, UPDATE that preallocated row instead of inserting a
    new one (avoids an orphan 'running' row + duplicate completed row).
    """
    async with aiosqlite.connect(db_path) as conn:
        await _ensure_schema(conn)
        cursor = await conn.execute("SELECT id FROM subjects WHERE slug = ?", (slug,))
        row = await cursor.fetchone()
        subject_id = row[0] if row else None
        if run_id is not None:
            await conn.execute(
                """
                UPDATE research_runs SET
                    subject_id = ?, thread_id = ?, topic = ?, research_brief = ?,
                    final_report = ?, sources = ?, raw_notes = ?, config = ?,
                    status = ?, error = ?
                WHERE id = ?
                """,
                (
                    subject_id,
                    run.get("thread_id"),
                    run.get("topic"),
                    run.get("research_brief"),
                    run.get("final_report"),
                    json.dumps(run.get("sources", [])),
                    json.dumps(run.get("raw_notes", [])),
                    json.dumps(run.get("config", {})),
                    run.get("status", "answered_from_cache"),
                    run.get("error"),
                    run_id,
                ),
            )
        else:
            run_cursor = await conn.execute(
                """
                INSERT INTO research_runs (
                    subject_id, thread_id, topic, research_brief, final_report,
                    sources, raw_notes, config, status, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    subject_id,
                    run.get("thread_id"),
                    run.get("topic"),
                    run.get("research_brief"),
                    run.get("final_report"),
                    json.dumps(run.get("sources", [])),
                    json.dumps(run.get("raw_notes", [])),
                    json.dumps(run.get("config", {})),
                    run.get("status", "answered_from_cache"),
                    run.get("error"),
                    run.get("created_at"),
                ),
            )
            run_id = run_cursor.lastrowid
        await conn.commit()
        return run_id


async def save_run_and_upsert_subject(
    db_path: str,
    *,
    subject_name: str,
    slug: str,
    merged_report: str,
    sources_union: list[str],
    run: dict,
    now: str,
    run_id: Optional[int] = None,
) -> tuple[int, int]:
    """Insert the run and upsert its subject's accumulated dossier, atomically.

    Returns ``(subject_id, run_id)``. On an existing subject the canonical name is
    kept; ``current_report``/``sources`` are replaced with the merged values and
    ``run_count`` is incremented.
    """
    async with aiosqlite.connect(db_path) as conn:
        await _ensure_schema(conn)
        await conn.execute(
            """
            INSERT INTO subjects (slug, name, current_report, sources, run_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                current_report = excluded.current_report,
                sources = excluded.sources,
                run_count = subjects.run_count + 1,
                updated_at = excluded.updated_at
            """,
            (slug, subject_name, merged_report, json.dumps(sources_union), now, now),
        )
        cursor = await conn.execute("SELECT id FROM subjects WHERE slug = ?", (slug,))
        subject_id = (await cursor.fetchone())[0]

        if run_id is not None:
            # Finalize the row preallocated at graph START (no duplicate insert).
            await conn.execute(
                """
                UPDATE research_runs SET
                    subject_id = ?, thread_id = ?, topic = ?, research_brief = ?,
                    final_report = ?, sources = ?, raw_notes = ?, config = ?,
                    status = ?, error = ?
                WHERE id = ?
                """,
                (
                    subject_id,
                    run.get("thread_id"),
                    run.get("topic"),
                    run.get("research_brief"),
                    run.get("final_report"),
                    json.dumps(run.get("sources", [])),
                    json.dumps(run.get("raw_notes", [])),
                    json.dumps(run.get("config", {})),
                    run.get("status", "completed"),
                    run.get("error"),
                    run_id,
                ),
            )
        else:
            run_cursor = await conn.execute(
                """
                INSERT INTO research_runs (
                    subject_id, thread_id, topic, research_brief, final_report,
                    sources, raw_notes, config, status, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    subject_id,
                    run.get("thread_id"),
                    run.get("topic"),
                    run.get("research_brief"),
                    run.get("final_report"),
                    json.dumps(run.get("sources", [])),
                    json.dumps(run.get("raw_notes", [])),
                    json.dumps(run.get("config", {})),
                    run.get("status", "completed"),
                    run.get("error"),
                    run.get("created_at", now),
                ),
            )
            run_id = run_cursor.lastrowid

        # Timestamped snapshot of the dossier as it stands after this update, so
        # the evolution of the subject's knowledge can be viewed historically.
        await conn.execute(
            """
            INSERT INTO dossier_versions (subject_id, run_id, dossier, sources, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (subject_id, run_id, merged_report, json.dumps(sources_union), now),
        )

        await conn.commit()
        return subject_id, run_id


async def get_dossier_history(db_path: str, slug: str) -> list[dict]:
    """Return the timestamped dossier snapshots for a subject, oldest first."""
    async with aiosqlite.connect(db_path) as conn:
        await _ensure_schema(conn)
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            """
            SELECT v.created_at, v.dossier, v.sources, v.run_id
            FROM dossier_versions v
            JOIN subjects s ON s.id = v.subject_id
            WHERE s.slug = ?
            ORDER BY v.created_at, v.id
            """,
            (slug,),
        )
        return [
            {
                "created_at": row["created_at"],
                "dossier": row["dossier"],
                "sources": json.loads(row["sources"] or "[]"),
                "run_id": row["run_id"],
            }
            for row in await cursor.fetchall()
        ]


async def preallocate_run(db_path: str, thread_id: str) -> int:
    """Insert a research_runs row with status='running' and return its id."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(db_path) as conn:
        await _ensure_schema(conn)
        from open_deep_research.factbase import migrations, schema
        await migrations.apply(conn, schema.STEPS)  # ensure lifecycle columns exist
        cur = await conn.execute(
            "INSERT INTO research_runs (thread_id, status, last_heartbeat, created_at) VALUES (?,?,?,?)",
            (thread_id, "running", now, now),
        )
        await conn.commit()
        return cur.lastrowid


async def finalize_research_run(db_path: str, run_id: int, fields: dict) -> None:
    """UPDATE the preallocated row to its terminal state. Only whitelisted columns."""
    allowed = {"status", "topic", "research_brief", "final_report", "sources",
               "raw_notes", "config", "error", "coverage_incomplete",
               "profile_name", "profile_version", "profile_hash"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    cols = ", ".join(f"{k}=?" for k in sets)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(f"UPDATE research_runs SET {cols} WHERE id=?", (*sets.values(), run_id))
        await conn.commit()


async def reap_stale_running(db_path: str, older_than_iso: str) -> int:
    """Mark stale 'running' runs (last_heartbeat < cutoff) as 'error'. Returns rows changed."""
    async with aiosqlite.connect(db_path) as conn:
        await _ensure_schema(conn)
        # last_heartbeat is added by the factbase migrations (not the base schema); apply
        # them so the reaper works on a fresh DB too (it can run before the first preallocate).
        from open_deep_research.factbase import migrations, schema
        await migrations.apply(conn, schema.STEPS)
        cur = await conn.execute(
            "UPDATE research_runs SET status='error', error='reaped: stale running run' "
            "WHERE status='running' AND last_heartbeat IS NOT NULL AND last_heartbeat < ?",
            (older_than_iso,),
        )
        await conn.commit()
        return cur.rowcount


async def set_coverage_incomplete(db_path: str, run_id: int, value: bool) -> None:
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("UPDATE research_runs SET coverage_incomplete=? WHERE id=?",
                           (1 if value else 0, run_id))
        await conn.commit()
