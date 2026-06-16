"""Structural rebuild: re-derive tuple_key, conflicts, and promotion for stored facts
after a profile's structural fields change (identity_qualifiers, value_enum,
required_qualifiers, trust_threshold, or a property rename/remove).

Reconstructs ``model.Fact`` objects from retained rows (recomputing source_meets_bar
and has_unspecified_required, which are not stored columns) and re-runs the SAME
``conflict.detect`` + ``promotion.evaluate`` as ingestion -- plus demotion, which
ingestion never does. Forward-only; preserves resolved (human-adjudicated) conflicts.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import aiosqlite

from . import conflict as _conflict, identity as _identity, model as _model, promotion as _promotion
from .recompute import backfill_canonical_values


async def rebuild_structural(conn: aiosqlite.Connection, profile, registry, *,
                             rename: dict | None = None, on_removed: str = "retain") -> dict:
    conn.row_factory = aiosqlite.Row
    now = datetime.now(timezone.utc).isoformat()
    stats = {"tuple_keys_changed": 0, "conflicts_opened": 0,
             "promoted": 0, "demoted": 0, "orphaned": 0}

    for old, new in (rename or {}).items():
        await conn.execute(
            "UPDATE fact SET property_name=? WHERE property_name=? AND soft_deleted_at IS NULL",
            (new, old))

    await backfill_canonical_values(conn, profile, force=True)

    rows = await (await conn.execute(
        "SELECT id, instance_key, property_name, qualifiers_json, as_of, value, unit, "
        "canonical_value, canonical_unit, source_id, admission, tuple_key "
        "FROM fact WHERE soft_deleted_at IS NULL")).fetchall()

    buckets: dict[tuple, list] = {}
    for r in rows:
        try:
            pd = profile.property(r["property_name"])
        except KeyError:
            stats["orphaned"] += 1
            if on_removed == "soft_delete":
                await conn.execute("UPDATE fact SET soft_deleted_at=? WHERE id=?", (now, r["id"]))
            continue
        quals = json.loads(r["qualifiers_json"] or "{}")
        ident = {q: quals.get(q) for q in pd.identity_qualifiers}
        new_tk = _identity.tuple_key(r["instance_key"], pd.name, ident)
        if new_tk != r["tuple_key"]:
            await conn.execute("UPDATE fact SET tuple_key=? WHERE id=?", (new_tk, r["id"]))
            stats["tuple_keys_changed"] += 1
        src = await (await conn.execute(
            "SELECT url_or_domain FROM source WHERE id=?", (r["source_id"],))).fetchone()
        url = src["url_or_domain"] if src else ""
        meets = registry.meets_bar(url, getattr(pd, "trust_threshold", "reputable"))
        has_unspec = any(ident.get(q) is None for q in (pd.required_qualifiers or []))
        f = _model.Fact(
            fact_id=r["id"], tuple_key=new_tk, as_of=r["as_of"], value=r["value"], unit=r["unit"],
            source_meets_bar=meets, has_unspecified_required=has_unspec, admission=r["admission"],
            canonical_value=r["canonical_value"], canonical_unit=r["canonical_unit"])
        buckets.setdefault((new_tk, r["as_of"]), []).append(f)

    await conn.execute(
        "DELETE FROM conflict_member WHERE conflict_id IN (SELECT id FROM conflict WHERE status='open')")
    await conn.execute("DELETE FROM conflict WHERE status='open'")

    for (tk, as_of), bucket in buckets.items():
        intents = _conflict.detect(bucket)
        has_open = False
        for intent in intents:
            if isinstance(intent, _model.OpenConflict):
                has_open = True
                cc = await conn.execute(
                    "INSERT INTO conflict (tuple_key, as_of, status, created_at) VALUES (?,?, 'open', ?)",
                    (tk, as_of, now))
                stats["conflicts_opened"] += 1
                for fid in intent.fact_ids:
                    await conn.execute(
                        "INSERT INTO conflict_member (conflict_id, fact_id) VALUES (?,?)",
                        (cc.lastrowid, fid))
        for f in bucket:
            ev = _promotion.evaluate(f, bucket, has_open_conflict=has_open)
            if isinstance(ev, _model.Promote):
                await conn.execute("UPDATE fact SET admission='trusted' WHERE id=?", (f.fact_id,))
                stats["promoted"] += 1
            elif isinstance(ev, _model.Demote):
                await conn.execute("UPDATE fact SET admission='provisional' WHERE id=?", (f.fact_id,))
                stats["demoted"] += 1

    await conn.commit()
    return stats
