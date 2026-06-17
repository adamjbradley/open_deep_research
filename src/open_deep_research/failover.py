"""Reactive model failover: error classification + per-run availability tracking.

A stage's model is resolved to a *chain* (primary first). On a hard-unavailable
error (quota exhausted, 404, auth) the caller fails over to the next model in the
chain and marks the dead model down for the rest of the run. Transient errors
(rate-limit throttle, overload, timeout) are retried on the same model by the
backend's own retry layer and never mark it down. Run state lives in a ContextVar
so it resets each run and is shared (read + mutate) across the concurrent
researcher fan-out within a run (asyncio is single-threaded, so set/list mutation
is race-safe).
"""
from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass, field

# Substrings that mark a failure as HARD-unavailable: the model will not recover
# this run, so fail over immediately rather than burning retries on it. Checked
# BEFORE transient markers so a quota-exhausted "429 ... quota" classifies hard.
_HARD_MARKERS = (
    "quota",
    "insufficient_quota",
    "resource_exhausted",
    "exhausted",
    "billing",
    "model not found",
    "model_not_found",
    "does not exist",
    "404",
    "unauthorized",
    "invalid api key",
    "invalid_api_key",
    "permission denied",
    "401",
    "403",
)

# Substrings that mark a failure as TRANSIENT: a blip/throttle worth retrying.
_TRANSIENT_MARKERS = (
    "rate limit",
    "rate_limit",
    " 429",
    "overloaded",
    "overload",
    "503",
    "connection reset",
    "connection error",
    "broken pipe",
    "timed out",
    "timeout",
)


def classify_error(exc: BaseException) -> str:
    """Classify a model-call failure as 'hard' or 'transient'.

    'hard'      -> model unavailable for the rest of the run; fail over now.
    'transient' -> blip/throttle; retry the SAME model first.

    A hard marker (quota/404/auth) wins even when a transient marker (a bare 429)
    is also present. Anything unrecognised defaults to 'transient' so an ambiguous
    error is retried first and only escalates to a failover if it persists.
    """
    if isinstance(exc, asyncio.TimeoutError | TimeoutError):
        return "transient"
    text = str(exc).lower()
    if any(m in text for m in _HARD_MARKERS):
        return "hard"
    if any(m in text for m in _TRANSIENT_MARKERS):
        return "transient"
    return "transient"


def reason_for(exc: BaseException, kind: str) -> str:
    """A short, single-line reason string for logs + the run record."""
    text = str(exc).strip()
    first = text.splitlines()[0] if text else exc.__class__.__name__
    return f"{kind}: {first}"[:140]
