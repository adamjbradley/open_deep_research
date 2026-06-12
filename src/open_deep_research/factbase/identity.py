from __future__ import annotations
import hashlib
import re

_WS = re.compile(r"\s+")


def canonicalize(value: str, unit: str | None) -> str:
    v = _WS.sub(" ", (value or "").strip().lower())
    u = _WS.sub(" ", (unit or "").strip().lower())
    return f"{v}␟{u}"  # value/unit separator keeps them distinct


def values_equal(a_value: str, a_unit: str | None, b_value: str, b_unit: str | None) -> bool:
    return canonicalize(a_value, a_unit) == canonicalize(b_value, b_unit)


def tuple_key(instance_id: int, property_name: str, qualifiers: dict[str, str | None]) -> str:
    """Hash of (instance, property, sorted non-temporal qualifiers).
    as_of is the version axis and is deliberately NOT a parameter. A None qualifier
    value renders as the literal 'unspecified' so such a fact gets its own tuple."""
    parts = [str(instance_id), property_name]
    for name in sorted(qualifiers):
        val = qualifiers[name]
        parts.append(f"{name}={'unspecified' if val is None else val.strip().lower()}")
    raw = "␞".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
