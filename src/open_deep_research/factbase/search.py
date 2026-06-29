"""Keyword search over the research substrate (FTS5).

Read-only query layer. Hides the FTS engine behind ``search_research`` so a
semantic adapter can replace it later without changing callers. Freshness/trust
fields ride along on every hit but are never used to filter here.
"""
from __future__ import annotations

import csv as _csv
import io
from dataclasses import dataclass

import aiosqlite

from . import search_schema
from .entities import CountryResolver

_SNIPPET = "snippet({tbl}, 0, '[', ']', '…', 12)"


@dataclass
class Hit:
    kind: str                       # "source" | "fact"
    ref_id: int                     # base-table row id
    subject: str | None             # canonical alpha-3 country key
    snippet: str
    score: float                    # higher = more relevant (−bm25)
    source_url: str | None = None
    title: str | None = None
    property_name: str | None = None
    as_of: object = None
    lifecycle: str | None = None
    admission: str | None = None
    retrieved_at: str | None = None


def _to_match(query: str) -> str | None:
    """Quote each whitespace token as an FTS5 literal so user input can't be a
    syntax error (a bare ``"`` or operator). Returns None if nothing usable."""
    tokens = [t for t in (query or "").split() if t.strip()]
    quoted = ['"' + t.replace('"', '""') + '"' for t in tokens]
    quoted = [q for q in quoted if q != '""']
    return " ".join(quoted) or None


async def _resolve_subject(name: str | None) -> str | None:
    return CountryResolver().resolve(name) if name else None


async def _source_hits(conn, match, target, limit):
    sql = f"""
        SELECT rs.id, rs.source_url, rs.title, rs.retrieved_at, rs.thread_id,
               s.name AS subject_name,
               bm25(fts_source) AS score, {_SNIPPET.format(tbl='fts_source')} AS snip
        FROM fts_source
        JOIN run_source rs ON rs.id = fts_source.rowid
        LEFT JOIN research_runs r ON r.thread_id = rs.thread_id
        LEFT JOIN subjects s ON s.id = r.subject_id
        WHERE fts_source MATCH ? AND rs.soft_deleted_at IS NULL
        ORDER BY score LIMIT ?
    """
    conn.row_factory = aiosqlite.Row
    cur = await conn.execute(sql, (match, limit))
    out = []
    for row in await cur.fetchall():
        subj = CountryResolver().resolve(row["subject_name"]) if row["subject_name"] else None
        if target is not None and subj != target:
            continue
        out.append(Hit(kind="source", ref_id=row["id"], subject=subj,
                       snippet=row["snip"], score=-row["score"],
                       source_url=row["source_url"], title=row["title"],
                       retrieved_at=row["retrieved_at"]))
    return out


async def _fact_hits(conn, match, target, limit):
    sql = f"""
        SELECT f.id, f.instance_key, f.property_name, f.as_of, f.lifecycle, f.admission,
               bm25(fts_fact) AS score, {_SNIPPET.format(tbl='fts_fact')} AS snip
        FROM fts_fact
        JOIN fact f ON f.id = fts_fact.rowid
        WHERE fts_fact MATCH ? AND f.soft_deleted_at IS NULL
        {{subject}}
        ORDER BY score LIMIT ?
    """
    params: list = [match]
    subject_clause = ""
    if target is not None:
        subject_clause = "AND f.instance_key = ?"
        params.append(target)
    params.append(limit)
    conn.row_factory = aiosqlite.Row
    cur = await conn.execute(sql.format(subject=subject_clause), tuple(params))
    return [Hit(kind="fact", ref_id=row["id"], subject=row["instance_key"],
                snippet=row["snip"], score=-row["score"],
                property_name=row["property_name"], as_of=row["as_of"],
                lifecycle=row["lifecycle"], admission=row["admission"])
            for row in await cur.fetchall()]


async def search_research(conn, query, *, subject=None, kinds=("source", "fact"), limit=20):
    """Keyword-search the substrate. Returns ranked Hits (higher score = better).

    Cross-kind scores are both −bm25 and only approximately comparable in v1.
    """
    await search_schema.ensure_search_schema(conn)
    match = _to_match(query)
    if match is None:
        return []
    target = await _resolve_subject(subject)
    hits: list[Hit] = []
    try:
        if "source" in kinds:
            hits += await _source_hits(conn, match, target, limit)
        if "fact" in kinds:
            hits += await _fact_hits(conn, match, target, limit)
    except aiosqlite.OperationalError:
        return []  # FTS syntax edge cases degrade to "no results", never raise
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]


def format_hits(hits, fmt: str = "text") -> str:
    if not hits:
        return "(no results)"
    def _detail(h):
        return h.source_url if h.kind == "source" else f"{h.subject}/{h.property_name}"
    def _fresh(h):
        return f"[{h.lifecycle},{h.admission}]" if h.kind == "fact" else ""
    if fmt == "csv":
        buf = io.StringIO()
        w = _csv.writer(buf)
        w.writerow(["kind", "subject", "score", "detail", "snippet"])
        for h in hits:
            w.writerow([h.kind, h.subject or "", f"{h.score:.3f}", _detail(h), h.snippet])
        return buf.getvalue().rstrip("\n")
    if fmt == "md":
        lines = ["| score | kind | subject | detail | snippet |", "|---|---|---|---|---|"]
        for h in hits:
            snip = (h.snippet or "").replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {h.score:+.3f} | {h.kind} | {h.subject or ''} | {_detail(h)} | {snip} |")
        return "\n".join(lines)
    lines = []
    for h in hits:
        lines.append(f"{h.score:+.3f}  {h.kind:<6} {_detail(h)} {_fresh(h)}\n        {h.snippet}")
    return "\n".join(lines)
