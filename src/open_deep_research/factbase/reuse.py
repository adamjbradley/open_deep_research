"""KB-first reuse predicate: is a property's current value good enough to skip researching?

Conservative: a trusted (admission), unconflicted value captured within a freshness window.
Trust and recency are evaluated on the SAME rows via `trusted_captured_at` (the newest capture
among the group's trusted rows), so a stale trusted row + a fresh provisional row can't qualify.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def is_reusable(group_row: dict, *, now: datetime, max_age_days: int) -> bool:
    """Return True iff this grouped row's trusted value is unconflicted and within the freshness window."""
    if group_row.get("in_conflict"):
        return False
    captured = _parse(group_row.get("trusted_captured_at"))
    if captured is None:
        return False
    age_days = (now - captured).days
    return 0 <= age_days <= max_age_days   # lower bound: a future capture (clock skew) is NOT fresh


def is_property_reusable(rows: list[dict], *, now: datetime, max_age_days: int) -> bool:
    """Return True iff this property's grouped rows are reusable.

    Property-level reuse over ALL grouped rows for one property (a property can have several
    grouped rows for different qualifiers / as_of). Conservative: reusable iff SOME row is
    trusted+recent AND NO row is in conflict.
    """
    if not rows:
        return False
    if any(r.get("in_conflict") for r in rows):
        return False
    return any(is_reusable(r, now=now, max_age_days=max_age_days) for r in rows)
