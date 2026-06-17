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


@dataclass(eq=True)
class FailoverRecord:
    """One failover event, for logging + persistence on the run."""

    stage: str
    from_model: str
    to_model: str
    reason: str

    def as_dict(self) -> dict:
        return {"stage": self.stage, "from": self.from_model,
                "to": self.to_model, "reason": self.reason}


@dataclass
class AvailabilityTracker:
    """Run-scoped record of models marked down (hard-failed) + failover events.

    One asyncio thread per process, so plain set/list mutation is race-safe even
    when the researcher fan-out shares a single tracker within a run.
    """

    _down: set[str] = field(default_factory=set)
    failovers: list[FailoverRecord] = field(default_factory=list)

    def is_down(self, model: str) -> bool:
        return model in self._down

    def mark_down(self, model: str) -> None:
        self._down.add(model)

    def available_chain(self, chain: list[str]) -> list[str]:
        """The chain with already-down models removed (order preserved)."""
        return [m for m in chain if m not in self._down]

    def record_failover(self, stage: str, from_model: str, to_model: str,
                        reason: str) -> None:
        self.failovers.append(FailoverRecord(stage, from_model, to_model, reason))


_current_tracker: contextvars.ContextVar["AvailabilityTracker | None"] = \
    contextvars.ContextVar("odr_failover_tracker", default=None)

# Module-level registry keyed by thread_id (or any run key).  Module globals are
# shared across all LangGraph node contexts (unlike ContextVars, which each node
# sees in a *copied* context), so this survives the node-boundary that broke the
# ContextVar-only approach.
_registry: dict[str, AvailabilityTracker] = {}


def new_run_tracker(key: str | None = None) -> AvailabilityTracker:
    """Install a fresh tracker for the current run and return it.

    When ``key`` (e.g. the run's thread_id) is given, the tracker is stored in a
    module-level registry so it survives across LangGraph nodes (each node runs in
    its own copied context, so a ContextVar alone would not). Always also set the
    ContextVar so key-less ``get_tracker()`` in the same context still works. A
    fresh tracker overwrites any previous one for the same key, so re-running with a
    reused thread_id starts clean (run-scoped).
    """
    t = AvailabilityTracker()
    if key is not None:
        _registry[key] = t
    _current_tracker.set(t)
    return t


def get_tracker(key: str | None = None) -> AvailabilityTracker:
    """The current run's tracker.

    With ``key`` (thread_id): return the registry's tracker for that run, creating
    and storing one if absent. Without a key: fall back to the ContextVar (lazily
    creating a detached tracker), preserving the original single-context behaviour
    used by unit tests and key-less callers.
    """
    if key is not None:
        t = _registry.get(key)
        if t is None:
            t = AvailabilityTracker()
            _registry[key] = t
        return t
    t = _current_tracker.get()
    if t is None:
        t = AvailabilityTracker()
        _current_tracker.set(t)
    return t


def discard_tracker(key: str | None) -> None:
    """Drop a run's tracker from the registry (call after persisting). No-op if absent."""
    if key is not None:
        _registry.pop(key, None)
