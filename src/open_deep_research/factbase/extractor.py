"""Per-source fact extraction with post-coercion validation + span verification.

``model_call(source_text, profile) -> list[dict]`` is injected so this is
unit-testable without a live model.
"""
from __future__ import annotations

import re

from .profile import Profile

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").strip().lower())


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
        ok = True
        for q, v in (rec.get("qualifiers") or {}).items():
            if v is None:
                continue
            allowed = pd.qualifier_enums.get(q)
            if allowed is not None and v.lower() not in {a.lower() for a in allowed}:
                ok = False
                break
        if ok:
            kept.append(rec)
    return kept
