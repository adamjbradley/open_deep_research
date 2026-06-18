"""Lean per-source extraction: the simplified record the model emits + deterministic
reconstruction of the rich qualifiers dict. Keeping the open-ended qualifiers OUT of the
model's structured output is what lets a cheap model emit it reliably."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


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
