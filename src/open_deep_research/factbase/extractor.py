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


def _span_present(span_norm: str, source_norm: str) -> bool:
    """Span verification: exact substring, else a high-similarity window match.

    The fuzzy fallback rescues paraphrased/whitespace-mangled quotes that are still
    substantially present, without admitting hallucinated spans (threshold is strict).
    """
    if not span_norm:
        return False
    if span_norm in source_norm:
        return True
    n = len(span_norm)
    m = len(source_norm)
    if n < 12:  # too short to fuzz safely
        return False
    # When span is longer than source (e.g. span adds "the"), do a direct comparison.
    if n > m:
        # Reject if span vastly exceeds source length (likely hallucinated extra content).
        if n > m * 1.25:
            return False
        return difflib.SequenceMatcher(None, span_norm, source_norm).ratio() >= _FUZZY_THRESHOLD
    # Slide a window of the span's length across the source; accept on a strong ratio.
    step = max(1, n // 4)
    for i in range(0, m - n + 1, step):
        window = source_norm[i:i + n]
        if difflib.SequenceMatcher(None, span_norm, window).ratio() >= _FUZZY_THRESHOLD:
            return True
    return False


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
        if not _span_present(_norm(span), norm_source):
            continue
        if not pd.validate(rec.get("value", "")):
            continue
        out = dict(rec)
        out["qualifiers"] = slot_qualifiers(pd, rec.get("qualifiers") or [])  # list -> dict
        kept.append(out)
    return kept
