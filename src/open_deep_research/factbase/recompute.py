"""Backfill canonical_value/canonical_unit on existing fact rows.

The value-normalization columns were added by migration v5; rows ingested before that
(or before an alias-map change) have NULL canonical_value. This recomputes them from the
profile so dedup/conflict/rendering see them consistently. Idempotent: only touches NULL
rows unless ``force=True``. Read paths tolerate NULL, so running this is optional/lazy.
"""
from __future__ import annotations

import aiosqlite

from . import identity


async def backfill_canonical_values(conn: aiosqlite.Connection, profile, *, force: bool = False) -> int:
    """Populate canonical_value/canonical_unit for fact rows. Returns rows updated.

    ``force=True`` recomputes every (non-deleted) row -- use after changing the alias map
    or normalization rules.
    """
    conn.row_factory = aiosqlite.Row
    where = "soft_deleted_at IS NULL" if force else "canonical_value IS NULL AND soft_deleted_at IS NULL"
    rows = await (await conn.execute(
        f"SELECT id, property_name, value, unit FROM fact WHERE {where}")).fetchall()

    updated = 0
    for row in rows:
        prop = row["property_name"]
        if not prop:
            continue  # legacy row without property_name; leave NULL
        try:
            pd = profile.property(prop)
        except KeyError:
            continue  # property no longer in the profile; leave NULL
        cval, cunit = identity.canonical_value(pd, row["value"], row["unit"])
        await conn.execute(
            "UPDATE fact SET canonical_value=?, canonical_unit=? WHERE id=?",
            (cval, cunit, row["id"]),
        )
        updated += 1
    await conn.commit()
    return updated
