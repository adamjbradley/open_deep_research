"""Per-source fact extraction with post-coercion validation + span verification.

``model_call(source_text, profile) -> list[dict]`` is injected so this is
unit-testable without a live model.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

from .lean_extract import slot_qualifiers
from .profile import Profile

_WS = re.compile(r"\s+")
_FUZZY_THRESHOLD = 0.9


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = s.replace(" ", " ").replace("“", '"').replace("”", '"') \
         .replace("‘", "'").replace("’", "'").replace("–", "-").replace("—", "-")
    return _WS.sub(" ", s.strip().lower())


async def extract(source_text: str, prof: Profile, model_call) -> list[dict]:
    raw = await model_call(source_text, prof)
    norm_source = _norm(source_text)
    kept: list[dict] = []
    for rec in raw or []:
        try:
            pd = prof.property(rec["property"])
        except KeyError:
            continue
        span = rec.get("evidence_span", "")
        if not span or _norm(span) not in norm_source:
            continue
        if not pd.validate(rec.get("value", "")):
            continue
        out = dict(rec)
        out["qualifiers"] = slot_qualifiers(pd, rec.get("qualifiers") or [])  # list -> dict
        kept.append(out)
    return kept
