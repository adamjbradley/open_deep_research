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


# Gap severity for per-round prioritization: a property with NO value is a bigger gap than one
# that only lacks a required qualifier, which is bigger than one that only lacks a narrative.
_SEVERITY_RANK = {"missing_value": 0, "missing_qualifier": 1, "missing_narrative": 2}


def order_incomplete_by_severity(incomplete: list, ledger: dict) -> list:
    """Order incomplete property names biggest-gap-first.

    So a bounded gap round spends its research budget on the properties that need the most
    (missing_value before missing_qualifier before missing_narrative). A stable sort preserves
    profile order within a severity tier; unknown statuses sort last.
    """
    return sorted(incomplete, key=lambda p: _SEVERITY_RANK.get(ledger.get(p), 99))


def is_complete(status: str, pd) -> bool:
    if status == "resolved":
        return True
    if status == "confirmed_absent":
        return bool(getattr(pd, "absence_allowed", True))
    return False
