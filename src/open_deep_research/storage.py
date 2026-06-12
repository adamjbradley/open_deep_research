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
            "SELECT id, name, current_report, sources FROM subjects WHERE slug = ?",
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
        }


async def save_run_and_upsert_subject(
    db_path: str,
    *,
    subject_name: str,
    slug: str,
    merged_report: str,
    sources_union: list[str],
    run: dict,
    now: str,
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
