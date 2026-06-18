"""Lean per-source extraction: the simplified record the model emits + deterministic
reconstruction of the rich qualifiers dict. Keeping the open-ended qualifiers OUT of the
model's structured output is what lets a cheap model emit it reliably."""
from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, Field


def _first_json_array(text: str) -> str | None:
    """The first balanced top-level JSON array in text (respecting string literals), else None."""
    start = text.find("[")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("[", start + 1)
    return None


class LeanFact(BaseModel):
    """What the model emits per fact: FactRecord with qualifiers as a FLAT list of enum
    tokens (e.g. ["total_pop", "issued"]) instead of a nested {qualifier: value} dict."""

    property: str
    instance_name: str
    value: str
    unit: Optional[str] = None
    as_of: Optional[str] = None
    evidence_span: str
    narrative: Optional[str] = None
    qualifiers: list[str] = Field(default_factory=list)


def slot_qualifiers(property_def, tokens: list[str]) -> dict:
    """Slot a flat list of qualifier enum tokens into {qualifier: value}.

    Enum values are disjoint across a property's qualifiers, so each token maps to exactly
    one slot. Tokens not in any of this property's qualifier_enums are dropped. Matching is
    case-insensitive; the canonical (lowercased) token is stored.
    """
    out: dict = {}
    for q, allowed in (getattr(property_def, "qualifier_enums", {}) or {}).items():
        allowed_lc = {a.lower() for a in allowed}
        for t in tokens or []:
            if t and t.strip().lower() in allowed_lc:
                out[q] = t.strip().lower()
                break
    return out


def parse_lean_facts(raw: str) -> list[dict]:
    """Lenient parse of the model's output into valid LeanFact dicts.

    Extracts the first JSON array from the text (tolerating markdown fences / surrounding
    prose), then validates each element against LeanFact INDEPENDENTLY -- keeping the valid
    records and skipping malformed ones (no all-or-nothing). Returns [] if nothing parses.
    """
    if not raw or not raw.strip():
        return []
    text = raw.strip()
    arr = None
    try:
        obj = json.loads(text)
        arr = obj if isinstance(obj, list) else obj.get("facts") if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001
        cand = _first_json_array(text)
        if cand:
            try:
                arr = json.loads(cand)
            except Exception:  # noqa: BLE001
                arr = None
        else:
            arr = None
    if not isinstance(arr, list):
        return []
    out: list[dict] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        try:
            out.append(LeanFact.model_validate(item).model_dump())
        except Exception:  # noqa: BLE001 - one bad record never drops the rest
            continue
    return out
