from __future__ import annotations
import hashlib
import math
import re

_WS = re.compile(r"\s+")


def canonicalize(value: str, unit: str | None) -> str:
    v = _WS.sub(" ", (value or "").strip().lower())
    u = _WS.sub(" ", (unit or "").strip().lower())
    return f"{v}␟{u}"  # value/unit separator keeps them distinct


# --- value normalization (dedup of semantically-equal-but-textually-different values) ---
_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_PAREN = re.compile(r"\([^)]*\)")
_YEAR_SUFFIX = re.compile(r"[,\s]+\d{4}\s*$")
_NON_WORD = re.compile(r"[^\w\s]")
# Trailing noise words that don't change a scheme/law's identity ("Aadhaar Card" == "Aadhaar").
_NOISE_WORDS = {"card", "scheme", "act", "system", "program", "programme", "number", "id"}
_ARTICLES = {"the", "a", "an"}


def _norm_text(s: str | None) -> str:
    return _WS.sub(" ", (s or "").strip().lower())


def _parse_percent(raw: str) -> str | None:
    """First numeric token in 0..100 -> a canonical string ('~99'/'99%'/'99 percent' -> '99').

    Returns None if no in-range number parses, so the caller falls back to text (we never
    invent a number)."""
    m = _NUM.search(raw or "")
    if not m:
        return None
    try:
        f = float(m.group())
    except ValueError:
        return None
    if not (0.0 <= f <= 100.0):
        return None
    return str(int(f)) if f == int(f) else str(f)


def canonical_value(property_def, value: str, unit: str | None) -> tuple[str, str | None]:
    """Canonical (value, unit) for dedup / conflict grouping, by ``property_def.value_kind``.

    Deterministic and never raises. The RAW value is preserved by callers; this is only the
    grouping key. Unknown/None property_def falls back to plain lower+whitespace normalization.
    """
    raw = value or ""
    kind = getattr(property_def, "value_kind", None)

    if kind == "percentage":
        num = _parse_percent(raw)
        return (num, "%") if num is not None else (_norm_text(raw), _norm_text(unit) or None)

    if kind == "enum":
        v = _norm_text(raw)
        for e in (getattr(property_def, "value_enum", None) or []):
            if v == e.strip().lower():
                return (v, None)
        return (v, _norm_text(unit) or None)  # out-of-enum: don't collapse into a member

    if kind == "boolean":
        v = _norm_text(raw)
        if v in {"true", "yes", "1", "enacted", "in force", "in_force", "present"}:
            return ("true", None)
        if v in {"false", "no", "0", "absent", "none", ""}:
            return ("false", None)
        return (v, _norm_text(unit) or None)

    if kind == "number":
        s = raw.replace(",", "").replace("_", "").replace(" ", "")
        try:
            f = float(s)
        except ValueError:
            f = None
        if f is None or not math.isfinite(f):  # non-numeric or inf/nan -> text fallback (never raise)
            return (_norm_text(raw), _norm_text(unit) or None)
        canon = str(int(f)) if f == int(f) else repr(f)
        return (canon, _norm_text(unit) or None)

    if kind in ("name", "name_year"):
        v = _norm_text(raw)
        v = _PAREN.sub(" ", v)
        if kind == "name_year":
            v = _YEAR_SUFFIX.sub("", v)
        v = _WS.sub(" ", _NON_WORD.sub(" ", v)).strip()
        toks = [t for t in v.split(" ") if t and t not in _ARTICLES]
        while toks and toks[-1] in _NOISE_WORDS:  # strip only TRAILING noise words
            toks.pop()
        v = " ".join(toks)
        aliases_for = getattr(property_def, "aliases_for", None)
        if callable(aliases_for):
            v = aliases_for(v) or v
        return (v, _norm_text(unit) or None)

    return (_norm_text(raw), _norm_text(unit) or None)


def values_equal(a_value: str, a_unit: str | None, b_value: str, b_unit: str | None) -> bool:
    return canonicalize(a_value, a_unit) == canonicalize(b_value, b_unit)


def tuple_key(instance_id: int | str, property_name: str, qualifiers: dict[str, str | None]) -> str:
    """Hash of (instance, property, sorted non-temporal qualifiers).
    as_of is the version axis and is deliberately NOT a parameter. A None qualifier
    value renders as the literal 'unspecified' so such a fact gets its own tuple."""
    parts = [str(instance_id), property_name]
    for name in sorted(qualifiers):
        val = qualifiers[name]
        parts.append(f"{name}={'unspecified' if val is None else val.strip().lower()}")
    raw = "␞".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
