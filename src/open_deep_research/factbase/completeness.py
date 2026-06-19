"""Per-property completeness ledger for whole-profile facts gathering (pure functions)."""
from __future__ import annotations

# A corroborated provisional counts as resolved (trusted preferred); requiring trusted for
# every property would never terminate. >=2 sources is the provisional bar.
_MIN_PROVISIONAL_SOURCES = 2


def _value_ok(rows) -> bool:
    for r in rows:
        if not str(r.get("value") or "").strip():
            continue
        if r.get("admission") == "trusted":
            return True
        if int(r.get("source_count") or 0) >= _MIN_PROVISIONAL_SOURCES:
            return True
    return False


def assess_property_status(grouped_rows, absent, prof) -> dict:
    by_prop = {}
    for r in grouped_rows:
        by_prop.setdefault(r.get("property_name"), []).append(r)
    out = {}
    for pd in prof.properties:
        p = pd.name
        if p in (absent or set()):
            out[p] = "confirmed_absent"
            continue
        rows = by_prop.get(p) or []
        if not _value_ok(rows):
            out[p] = "missing_value"
            continue
        # qualifiers: the chosen value row must carry every required qualifier
        req = set(getattr(pd, "required_qualifiers", []) or [])
        if req and not any(req <= set((r.get("qualifiers") or {}).keys()) for r in rows):
            out[p] = "missing_qualifier"
            continue
        if getattr(pd, "narrative_required", False) and not any(
                str(r.get("narrative") or "").strip() for r in rows):
            out[p] = "missing_narrative"
            continue
        out[p] = "resolved"
    return out


def is_complete(status: str, pd) -> bool:
    if status == "resolved":
        return True
    if status == "confirmed_absent":
        return bool(getattr(pd, "absence_allowed", True))
    return False
