# Model Failover Chains + Reactive Failover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every graph stage a backup-model chain that the run reactively fails over to when the primary is unavailable (quota/404/auth), with error-class routing and run-scoped "sticky down" tracking.

**Architecture:** A new pure `failover.py` owns policy (error classifier + per-run `AvailabilityTracker` in a `ContextVar`). The existing `configurable_claude_model` (the single chokepoint every stage flows through) resolves a *chain* instead of one model and, on a surfaced error, classifies it and advances to the next available model. Routing data (`model_routing.json`) gains `str | list[str]` role specs, resolved by a new `model_chain()` that keeps the old `resolve_model()` working as its head. Failovers are logged and ride along in the run's persisted `config` JSON.

**Tech Stack:** Python 3.10+, Pydantic v2, LangGraph/LangChain Runnables, pytest, the Claude/Gemini/Codex CLI backends in `claude_agent_chat.py`.

**Spec:** `docs/superpowers/specs/2026-06-17-model-failover-chains-design.md`

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/open_deep_research/failover.py` | Error classification, run-scoped availability tracking, failover records. Pure, no I/O. | **Create** |
| `src/open_deep_research/model_routing.py` | Accept `str \| list[str]` role/step specs; add `model_chain()`; `resolve_model()` returns the chain head. | Modify |
| `src/open_deep_research/configuration.py` | Add `Configuration.model_chain(role, step)` accessor mirroring `model_for`. | Modify |
| `src/open_deep_research/claude_agent_chat.py` | `configurable_claude_model`: capture `model_chain`/`stage`, run the failover loop in `ainvoke`, honour down-marks in `astream`. | Modify |
| `src/open_deep_research/deep_researcher.py` | Install a fresh tracker at run entry; thread `model_chain`+`stage` into each `*_model_config`; attach failovers to the persisted config. | Modify |
| `tests/test_failover.py` | Unit tests: classifier table, tracker stickiness, chain resolution. | **Create** |
| `tests/test_failover_integration.py` | Integration: fake backend hard-fails → lands on backup; transient retries same; record captured. | **Create** |

**Scope note:** This plan ships the *mechanism* only. It does NOT add backup chains to the shipped `model_routing.json` presets (every role stays a single string, so default behaviour is unchanged). Authoring real per-stage backup pairs into a preset is a follow-up config change (see "Follow-up" at the end).

---

## Task 1: Error classifier (`failover.py`)

**Files:**
- Create: `src/open_deep_research/failover.py`
- Test: `tests/test_failover.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_failover.py
import asyncio

import pytest

from open_deep_research.failover import classify_error, reason_for


@pytest.mark.parametrize("message,expected", [
    # hard: model won't recover this run -> fail over now
    ("Error: 429 RESOURCE_EXHAUSTED: Quota exceeded for quota metric", "hard"),
    ("insufficient_quota: You exceeded your current quota", "hard"),
    ("billing hard limit reached", "hard"),
    ("404 model not found: gemini-2.0-pro", "hard"),
    ("The model `gpt-9` does not exist", "hard"),
    ("401 Unauthorized: invalid api key", "hard"),
    ("403 permission denied", "hard"),
    # transient: retry the same model first
    ("429 rate limit exceeded, please slow down", "transient"),
    ("overloaded_error: the service is overloaded", "transient"),
    ("503 Service Unavailable", "transient"),
    ("connection reset by peer", "transient"),
    ("CLI gemini failed (exit 1): timed out", "transient"),
    # unknown -> default transient (retry first, escalate only if it persists)
    ("something totally unexpected happened", "transient"),
])
def test_classify_error_table(message, expected):
    assert classify_error(Exception(message)) == expected


def test_timeout_is_transient():
    assert classify_error(asyncio.TimeoutError()) == "transient"
    assert classify_error(TimeoutError()) == "transient"


def test_quota_429_beats_rate_limit_429():
    # a 429 that is really a quota exhaustion must classify HARD, not transient
    assert classify_error(Exception("429 quota exceeded")) == "hard"


def test_reason_for_is_short_single_line():
    exc = Exception("boom: line one\nline two\n" + "x" * 500)
    r = reason_for(exc, "hard")
    assert r.startswith("hard: boom: line one")
    assert "\n" not in r
    assert len(r) <= 140
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_failover.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'open_deep_research.failover'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/open_deep_research/failover.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_failover.py -q`
Expected: PASS (all parametrized cases + timeout + quota-429 + reason tests)

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/failover.py tests/test_failover.py
git commit -m "feat(failover): error classifier (hard vs transient)"
```

---

## Task 2: Availability tracker + run-scoped ContextVar (`failover.py`)

**Files:**
- Modify: `src/open_deep_research/failover.py`
- Test: `tests/test_failover.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_failover.py
from open_deep_research.failover import (
    AvailabilityTracker, FailoverRecord, get_tracker, new_run_tracker,
)


def test_tracker_mark_down_and_available_chain():
    t = AvailabilityTracker()
    chain = ["gemini:gemini-2.5-pro", "claude-opus-4-8"]
    assert t.available_chain(chain) == chain
    assert not t.is_down("gemini:gemini-2.5-pro")
    t.mark_down("gemini:gemini-2.5-pro")
    assert t.is_down("gemini:gemini-2.5-pro")
    assert t.available_chain(chain) == ["claude-opus-4-8"]


def test_tracker_records_failovers():
    t = AvailabilityTracker()
    t.record_failover("supervisor", "gemini:gemini-2.5-pro", "claude-opus-4-8", "hard: quota")
    assert t.failovers == [
        FailoverRecord("supervisor", "gemini:gemini-2.5-pro", "claude-opus-4-8", "hard: quota")
    ]
    assert t.failovers[0].as_dict() == {
        "stage": "supervisor", "from": "gemini:gemini-2.5-pro",
        "to": "claude-opus-4-8", "reason": "hard: quota",
    }


def test_new_run_tracker_resets_state():
    first = new_run_tracker()
    first.mark_down("gemini:gemini-2.5-flash")
    assert get_tracker() is first
    second = new_run_tracker()
    assert second is not first
    assert not second.is_down("gemini:gemini-2.5-flash")  # fresh run -> nothing down


def test_get_tracker_creates_detached_when_none():
    # simulate a clean context: a fresh ContextVar default is None
    import contextvars
    from open_deep_research import failover

    def _in_fresh_context():
        t = get_tracker()
        assert isinstance(t, AvailabilityTracker)
        assert get_tracker() is t  # stable within the context
        return "ok"

    ctx = contextvars.copy_context()
    # reset the var to its default inside the copied context, then run
    assert ctx.run(_in_fresh_context) == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_failover.py -q`
Expected: FAIL — `ImportError: cannot import name 'AvailabilityTracker'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/open_deep_research/failover.py`:

```python
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


def new_run_tracker() -> AvailabilityTracker:
    """Install a fresh tracker for the current run/context and return it.

    Call once at graph entry. Concurrent runs launched via ``asyncio.gather`` each
    run in a copied context, so each gets its own tracker.
    """
    t = AvailabilityTracker()
    _current_tracker.set(t)
    return t


def get_tracker() -> AvailabilityTracker:
    """The current run's tracker, lazily creating a detached one if none exists.

    A detached tracker still gives correct single-call behaviour (e.g. a one-off
    model call or a unit test); it simply shares state with nothing else.
    """
    t = _current_tracker.get()
    if t is None:
        t = AvailabilityTracker()
        _current_tracker.set(t)
    return t
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_failover.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/failover.py tests/test_failover.py
git commit -m "feat(failover): run-scoped AvailabilityTracker + FailoverRecord"
```

---

## Task 3: Chain-aware routing resolution (`model_routing.py`)

**Files:**
- Modify: `src/open_deep_research/model_routing.py` (`_check_model_string` area, `Preset._check`, add `_as_chain`/`_check_model_spec`/`model_chain`, rewrite `resolve_model`)
- Test: `tests/test_failover.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_failover.py
from open_deep_research.model_routing import (
    model_chain, resolve_model, routing_from_dict,
)


def _routing():
    return routing_from_dict({
        "version": "1", "active_preset": "mix",
        "presets": {"mix": {"roles": {
            "supervisor": ["gemini:gemini-2.5-pro", "claude-opus-4-8"],  # chain
            "researcher": "gemini:gemini-2.5-flash",                      # bare string
        }, "step_overrides": {"extract_facts": ["claude:sonnet", "gemini:gemini-2.5-flash"]}}},
    })


def test_list_spec_validates_and_resolves_to_chain():
    r = _routing()
    assert model_chain("supervisor", routing=r) == ["gemini:gemini-2.5-pro", "claude-opus-4-8"]


def test_string_spec_resolves_to_one_element_chain():
    r = _routing()
    assert model_chain("researcher", routing=r) == ["gemini:gemini-2.5-flash"]


def test_resolve_model_returns_chain_head_backcompat():
    r = _routing()
    assert resolve_model("supervisor", routing=r) == "gemini:gemini-2.5-pro"
    assert resolve_model("researcher", routing=r) == "gemini:gemini-2.5-flash"


def test_env_override_opts_out_of_failover():
    r = _routing()
    assert model_chain("supervisor", routing=r, env_value="claude:opus") == ["claude:opus"]


def test_step_override_chain_wins():
    r = _routing()
    assert model_chain("researcher", routing=r, step="extract_facts") == \
        ["claude:sonnet", "gemini:gemini-2.5-flash"]


def test_empty_chain_rejected():
    with pytest.raises(ValueError):
        routing_from_dict({
            "version": "1", "active_preset": "p",
            "presets": {"p": {"roles": {"supervisor": []}}},
        })
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_failover.py -q`
Expected: FAIL — `ImportError: cannot import name 'model_chain'` (and the list-spec routing raises a validation error under the current string-only `_check`)

- [ ] **Step 3: Write minimal implementation**

In `src/open_deep_research/model_routing.py`, add helpers after `_check_model_string` (after line 31):

```python
def _as_chain(spec: "str | list[str]") -> list[str]:
    """Normalise a model spec (string or list) to a primary-first chain."""
    return [spec] if isinstance(spec, str) else list(spec)


def _check_model_spec(spec: "str | list[str]", where: str) -> None:
    chain = _as_chain(spec)
    if not chain:
        raise ValueError(f"{where}: empty model chain")
    for model in chain:
        _check_model_string(model, where)
```

Change the `Preset` field types and `_check` body (lines 48 and 52-64). Replace:

```python
    roles: dict[str, str] = {}
    search: Optional[str] = None
    step_overrides: dict[str, str] = {}

    @model_validator(mode="after")
    def _check(self) -> Preset:
        for role, model in self.roles.items():
            if role not in KNOWN_ROLES:
                raise ValueError(f"unknown role {role!r} (known: {sorted(KNOWN_ROLES)})")
            _check_model_string(model, f"roles.{role}")
        for step, model in self.step_overrides.items():
            if step not in KNOWN_STEPS:
                raise ValueError(f"unknown step_override {step!r} (known: {sorted(KNOWN_STEPS)})")
            _check_model_string(model, f"step_overrides.{step}")
        if self.search is not None and self.search not in KNOWN_SEARCH:
            raise ValueError(f"unknown search {self.search!r} (known: {sorted(KNOWN_SEARCH)})")
        return self
```

with:

```python
    roles: dict[str, "str | list[str]"] = {}
    search: Optional[str] = None
    step_overrides: dict[str, "str | list[str]"] = {}

    @model_validator(mode="after")
    def _check(self) -> Preset:
        for role, spec in self.roles.items():
            if role not in KNOWN_ROLES:
                raise ValueError(f"unknown role {role!r} (known: {sorted(KNOWN_ROLES)})")
            _check_model_spec(spec, f"roles.{role}")
        for step, spec in self.step_overrides.items():
            if step not in KNOWN_STEPS:
                raise ValueError(f"unknown step_override {step!r} (known: {sorted(KNOWN_STEPS)})")
            _check_model_spec(spec, f"step_overrides.{step}")
        if self.search is not None and self.search not in KNOWN_SEARCH:
            raise ValueError(f"unknown search {self.search!r} (known: {sorted(KNOWN_SEARCH)})")
        return self
```

Add `model_chain` and rewrite `resolve_model` (replace the existing `resolve_model`, lines 128-142):

```python
def model_chain(role: str, *, routing: RoutingConfig | None = None, step: str | None = None,
                env_value: str | None = None, configurable_value: "str | list[str] | None" = None,
                code_default: str | None = None) -> list[str]:
    """Resolve a model failover chain (primary first): env > configurable > step_override > role > code default.

    Same precedence as ``resolve_model``; whatever wins is normalised to a list.
    An explicit env/configurable override yields a one-element chain (an override
    deliberately opts out of failover).
    """
    if env_value:
        return [env_value]
    if configurable_value is not None:
        return _as_chain(configurable_value)
    routing = routing or load_routing()
    preset = routing.active()
    if step and step in preset.step_overrides:
        return _as_chain(preset.step_overrides[step])
    if role in preset.roles:
        return _as_chain(preset.roles[role])
    return [code_default] if code_default else []


def resolve_model(role: str, *, routing: RoutingConfig | None = None, step: str | None = None,
                  env_value: str | None = None, configurable_value: str | None = None,
                  code_default: str | None = None) -> str | None:
    """Resolve a single model string (the chain head): env > configurable > step_override > role > code default."""
    chain = model_chain(role, routing=routing, step=step, env_value=env_value,
                        configurable_value=configurable_value, code_default=code_default)
    return chain[0] if chain else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_failover.py tests/test_model_routing_presets.py tests/test_model_routing_config_integration.py -q`
Expected: PASS — new chain tests green AND every existing routing/config test still green (back-compat: string presets resolve exactly as before).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/model_routing.py tests/test_failover.py
git commit -m "feat(routing): str|list[str] model specs + model_chain() resolver"
```

---

## Task 4: `Configuration.model_chain()` accessor

**Files:**
- Modify: `src/open_deep_research/configuration.py` (add method after `model_for`, around line 422)
- Test: `tests/test_failover.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_failover.py
import json

from open_deep_research.configuration import Configuration


def test_configuration_model_chain_uses_preset_list(monkeypatch, tmp_path):
    f = tmp_path / "model_routing.json"
    f.write_text(json.dumps({
        "version": "1", "active_preset": "mix",
        "presets": {"mix": {"roles": {
            "supervisor": ["gemini:gemini-2.5-pro", "claude-opus-4-8"],
            "researcher": "gemini:gemini-2.5-flash",
        }}},
    }), encoding="utf-8")
    monkeypatch.setenv("MODEL_ROUTING_FILE", str(f))
    for k in ("MODEL_ROUTING_PRESET", "SUPERVISOR_MODEL", "RESEARCHER_MODEL"):
        monkeypatch.delenv(k, raising=False)
    c = Configuration.from_runnable_config({})
    assert c.supervisor_model == "gemini:gemini-2.5-pro"          # head still on the field
    assert c.model_chain("supervisor") == ["gemini:gemini-2.5-pro", "claude-opus-4-8"]
    assert c.model_chain("researcher") == ["gemini:gemini-2.5-flash"]


def test_configuration_model_chain_env_override_is_single(monkeypatch, tmp_path):
    f = tmp_path / "model_routing.json"
    f.write_text(json.dumps({
        "version": "1", "active_preset": "mix",
        "presets": {"mix": {"roles": {"supervisor": ["gemini:gemini-2.5-pro", "claude-opus-4-8"]}}},
    }), encoding="utf-8")
    monkeypatch.setenv("MODEL_ROUTING_FILE", str(f))
    monkeypatch.delenv("MODEL_ROUTING_PRESET", raising=False)
    monkeypatch.setenv("SUPERVISOR_MODEL", "claude:opus")
    c = Configuration.from_runnable_config({})
    assert c.supervisor_model == "claude:opus"
    assert c.model_chain("supervisor") == ["claude:opus"]   # override opts out of failover


def test_configuration_model_chain_step_override(monkeypatch, tmp_path):
    f = tmp_path / "model_routing.json"
    f.write_text(json.dumps({
        "version": "1", "active_preset": "mix",
        "presets": {"mix": {
            "roles": {"researcher": "gemini:gemini-2.5-flash"},
            "step_overrides": {"extract_facts": ["claude:sonnet", "gemini:gemini-2.5-flash"]},
        }},
    }), encoding="utf-8")
    monkeypatch.setenv("MODEL_ROUTING_FILE", str(f))
    for k in ("MODEL_ROUTING_PRESET", "RESEARCHER_MODEL"):
        monkeypatch.delenv(k, raising=False)
    c = Configuration.from_runnable_config({})
    assert c.model_chain("researcher") == ["gemini:gemini-2.5-flash"]
    # the step override must drive the extract_facts chain, not the researcher role
    assert c.model_chain("researcher", "extract_facts") == ["claude:sonnet", "gemini:gemini-2.5-flash"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_failover.py -k model_chain -q`
Expected: FAIL — `AttributeError: 'Configuration' object has no attribute 'model_chain'`

- [ ] **Step 3: Write minimal implementation**

In `src/open_deep_research/configuration.py`, add this method directly after `model_for` (after line 422, before `class Config:`):

```python
    def model_chain(self, role: str, step: Optional[str] = None) -> list[str]:
        """The resolved failover chain (primary first) for a role/step.

        The head equals the resolved primary for that role/step (``model_for`` when
        a step is given, else the ``<role>_model`` field). If an env/configurable
        override set that primary, failover is intentionally off (single-element
        chain); otherwise the active preset's chain (role or step_override) is used.
        """
        from open_deep_research.model_routing import load_routing
        from open_deep_research.model_routing import model_chain as _model_chain
        primary = self.model_for(step, role) if step else getattr(self, f"{role}_model")
        if os.environ.get(f"{role}_model".upper()):
            return [primary]  # explicit env override opts out of failover
        chain = _model_chain(role, routing=load_routing(), step=step,
                             env_value=None, configurable_value=None, code_default=primary)
        # Trust the preset chain only if its head matches the resolved primary;
        # a mismatch means a configurable override set the primary -> no failover.
        return chain if chain and chain[0] == primary else [primary]
```

Confirm `Optional` is imported (it is used elsewhere in the file, e.g. `facts_answer_polish_model: Optional[str]`). No new import needed; `os` is already imported.

Also patch the existing `model_for` so a **list** step override returns its head (the method's contract is a single string; callers like `_make_fact_model_call` pass it straight to a model). Change its step-override branch (around line 420):

```python
        preset = load_routing().active()
        if step in preset.step_overrides:
            spec = preset.step_overrides[step]
            return spec if isinstance(spec, str) else spec[0]
        return getattr(self, f"{fallback_role}_model")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_failover.py -k model_chain -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/configuration.py tests/test_failover.py
git commit -m "feat(config): Configuration.model_chain() accessor"
```

---

## Task 5: Failover loop in `configurable_claude_model`

**Files:**
- Modify: `src/open_deep_research/claude_agent_chat.py` (`configurable_claude_model.with_config`, `_materialize`, `ainvoke`, `astream`; add `_resolve_chain`)
- Test: `tests/test_failover_integration.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_failover_integration.py
import asyncio

import pytest

from open_deep_research import claude_agent_chat as cac
from open_deep_research.claude_agent_chat import configurable_claude_model
from open_deep_research.failover import new_run_tracker


class _FakeModel:
    """A stand-in chat model whose ainvoke result/behaviour is scripted per model id."""

    def __init__(self, model_id, script):
        self.model_id = model_id
        self.script = script  # dict: model_id -> Exception instance or return value

    def with_structured_output(self, *a, **k):  # chainable no-ops used by the queue replay
        return self

    def bind_tools(self, *a, **k):
        return self

    def with_retry(self, *a, **k):
        return self

    async def ainvoke(self, *a, **k):
        outcome = self.script[self.model_id]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _patch_build(monkeypatch, script, constructed):
    def fake_build(model_string, max_tokens=None):
        constructed.append(model_string)
        return _FakeModel(model_string, script)
    monkeypatch.setattr(cac, "build_chat_model", fake_build)


def test_hard_error_fails_over_to_backup(monkeypatch):
    new_run_tracker()
    constructed = []
    script = {
        "gemini:gemini-2.5-pro": Exception("429 RESOURCE_EXHAUSTED: quota exceeded"),
        "claude-opus-4-8": "BACKUP-OK",
    }
    _patch_build(monkeypatch, script, constructed)
    model = configurable_claude_model().with_config({
        "model_chain": ["gemini:gemini-2.5-pro", "claude-opus-4-8"],
        "stage": "supervisor",
    })
    out = asyncio.run(model.ainvoke("hi"))
    assert out == "BACKUP-OK"
    assert constructed == ["gemini:gemini-2.5-pro", "claude-opus-4-8"]  # tried primary then backup


def test_sticky_skips_dead_primary_on_second_call(monkeypatch):
    tracker = new_run_tracker()
    constructed = []
    script = {
        "gemini:gemini-2.5-pro": Exception("quota exceeded"),
        "claude-opus-4-8": "OK",
    }
    _patch_build(monkeypatch, script, constructed)
    cfg = {"model_chain": ["gemini:gemini-2.5-pro", "claude-opus-4-8"], "stage": "supervisor"}
    model = configurable_claude_model().with_config(cfg)
    asyncio.run(model.ainvoke("a"))
    constructed.clear()
    asyncio.run(model.ainvoke("b"))
    assert constructed == ["claude-opus-4-8"]  # dead primary skipped second time
    assert tracker.is_down("gemini:gemini-2.5-pro")
    assert len(tracker.failovers) == 1  # only the first call recorded a failover event


def test_transient_does_not_mark_down(monkeypatch):
    tracker = new_run_tracker()
    constructed = []
    # transient on primary (surfaced past the backend's own retry) -> fail over for THIS
    # call, but the primary is NOT marked down (may recover later).
    script = {
        "gemini:gemini-2.5-flash": Exception("503 Service Unavailable"),
        "claude-haiku-4-5": "OK",
    }
    _patch_build(monkeypatch, script, constructed)
    model = configurable_claude_model().with_config({
        "model_chain": ["gemini:gemini-2.5-flash", "claude-haiku-4-5"], "stage": "researcher",
    })
    assert asyncio.run(model.ainvoke("x")) == "OK"
    assert not tracker.is_down("gemini:gemini-2.5-flash")


def test_single_model_chain_has_no_failover_and_raises(monkeypatch):
    new_run_tracker()
    constructed = []
    script = {"gemini:gemini-2.5-flash": Exception("quota exceeded")}
    _patch_build(monkeypatch, script, constructed)
    model = configurable_claude_model().with_config({
        "model_chain": ["gemini:gemini-2.5-flash"], "stage": "summarization",
    })
    with pytest.raises(Exception, match="quota exceeded"):
        asyncio.run(model.ainvoke("x"))


def test_exhausted_chain_raises_last_error(monkeypatch):
    new_run_tracker()
    constructed = []
    script = {
        "gemini:gemini-2.5-pro": Exception("quota exceeded"),
        "claude-opus-4-8": Exception("404 model not found"),
    }
    _patch_build(monkeypatch, script, constructed)
    model = configurable_claude_model().with_config({
        "model_chain": ["gemini:gemini-2.5-pro", "claude-opus-4-8"], "stage": "supervisor",
    })
    with pytest.raises(Exception, match="model not found"):
        asyncio.run(model.ainvoke("x"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_failover_integration.py -q`
Expected: FAIL — the current `ainvoke` ignores `model_chain` (it would construct the default `"gemini:gemini-2.5-flash"` model and `KeyError` on the script / not fail over).

- [ ] **Step 3: Write minimal implementation**

In `src/open_deep_research/claude_agent_chat.py`, extend `with_config` to capture the two new keys. In both capture loops (lines 1100 and 1104), change the key tuple:

```python
        for key in ("model", "max_tokens", "api_key", "model_chain", "stage"):
            if key in source:
                merged[key] = source[key]
        configurable = source.get("configurable") or {}
        for key in ("model", "max_tokens", "api_key", "model_chain", "stage"):
            if key in configurable:
                merged[key] = configurable[key]
```

Give `_materialize` an optional explicit-model override (replace lines 1119-1129):

```python
    def _materialize(self, config: Optional[RunnableConfig] = None,
                     model_override: Optional[str] = None) -> Runnable:
        cfg = dict(self._default_config)
        if config:
            configurable = config.get("configurable") or {}
            for key in ("model", "max_tokens", "api_key"):
                if key in configurable:
                    cfg[key] = configurable[key]
        model_string = model_override if model_override is not None else cfg.get("model")
        model: Runnable = build_chat_model(model_string, cfg.get("max_tokens"))
        for name, args, kwargs in self._queue:
            model = getattr(model, name)(*args, **kwargs)
        return model
```

Add a chain resolver helper (insert just above `ainvoke`, after `_materialize`):

```python
    def _resolve_chain(self, config: Optional[RunnableConfig] = None) -> tuple[list[str], str]:
        """The model chain to try (primary first) and the stage label for logging."""
        cfg = dict(self._default_config)
        if config:
            configurable = config.get("configurable") or {}
            for key in ("model", "model_chain", "stage"):
                if key in configurable:
                    cfg[key] = configurable[key]
        chain = cfg.get("model_chain") or ([cfg["model"]] if cfg.get("model") else [])
        chain = [m for m in chain if m]
        return chain, (cfg.get("stage") or "model")
```

Replace `ainvoke` (lines 1132-1133) with the failover loop:

```python
    async def ainvoke(self, input: Any, config: Optional[RunnableConfig] = None, **kwargs: Any):
        from open_deep_research.failover import classify_error, get_tracker, reason_for

        chain, stage = self._resolve_chain(config)
        if len(chain) <= 1:
            model_override = chain[0] if chain else None
            return await self._materialize(config, model_override=model_override).ainvoke(
                input, config, **kwargs)

        tracker = get_tracker()
        # Skip models already marked down this run; if all are down, still try the last
        # so a real error surfaces rather than silently returning nothing.
        available = tracker.available_chain(chain) or chain[-1:]
        last_exc: Optional[BaseException] = None
        for idx, model_string in enumerate(available):
            try:
                return await self._materialize(config, model_override=model_string).ainvoke(
                    input, config, **kwargs)
            except Exception as exc:  # noqa: BLE001 - re-raised below when the chain is exhausted
                last_exc = exc
                kind = classify_error(exc)
                if kind == "hard":
                    tracker.mark_down(model_string)  # sticky only for hard failures
                if idx >= len(available) - 1:
                    raise  # nothing left to fail over to
                next_model = available[idx + 1]
                reason = reason_for(exc, kind)
                tracker.record_failover(stage, model_string, next_model, reason)
                logger.warning("failover[%s]: %s unavailable (%s) -> %s",
                               stage, model_string, reason, next_model)
        raise last_exc  # unreachable: the loop raises on the last attempt
```

Make `astream` honour down-marks (replace lines 1138-1140). Streaming failover is out of scope; this just picks the first *available* model so a known-dead primary is skipped:

```python
    async def astream(self, input: Any, config: Optional[RunnableConfig] = None, **kwargs: Any):
        from open_deep_research.failover import get_tracker

        chain, _ = self._resolve_chain(config)
        model_override = None
        if chain:
            available = get_tracker().available_chain(chain) or chain
            model_override = available[0]
        async for chunk in self._materialize(config, model_override=model_override).astream(
                input, config, **kwargs):
            yield chunk
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_failover_integration.py -q`
Expected: PASS (failover to backup; sticky skip; transient not marked down; single-chain raises; exhausted chain raises last error)

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/claude_agent_chat.py tests/test_failover_integration.py
git commit -m "feat(failover): reactive failover loop in configurable_claude_model"
```

---

## Task 6: Wire chains, tracker, and persistence into the graph

**Files:**
- Modify: `src/open_deep_research/deep_researcher.py` (preallocate_run entry; each `*_model_config`; persist_research config)
- Test: `tests/test_failover_integration.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_failover_integration.py
from open_deep_research.configuration import Configuration


def test_node_style_config_shape_drives_failover(monkeypatch, tmp_path):
    """A config dict built the way a graph node builds it (model + model_chain +
    stage, sourced from a Configuration) actually engages reactive failover."""
    import json
    f = tmp_path / "model_routing.json"
    f.write_text(json.dumps({
        "version": "1", "active_preset": "mix",
        "presets": {"mix": {"roles": {
            "supervisor": ["gemini:gemini-2.5-pro", "claude-opus-4-8"],
        }}},
    }), encoding="utf-8")
    monkeypatch.setenv("MODEL_ROUTING_FILE", str(f))
    for k in ("MODEL_ROUTING_PRESET", "SUPERVISOR_MODEL"):
        monkeypatch.delenv(k, raising=False)
    c = Configuration.from_runnable_config({})
    assert c.model_chain("supervisor") == ["gemini:gemini-2.5-pro", "claude-opus-4-8"]

    new_run_tracker()
    constructed = []
    script = {
        "gemini:gemini-2.5-pro": Exception("429 quota exceeded"),
        "claude-opus-4-8": "BACKUP-OK",
    }
    _patch_build(monkeypatch, script, constructed)
    # exactly the keys a node attaches (see step 3c)
    model = configurable_claude_model().with_config({
        "model": c.supervisor_model,
        "model_chain": c.model_chain("supervisor"),
        "stage": "supervisor",
    })
    assert asyncio.run(model.ainvoke("hi")) == "BACKUP-OK"
    assert constructed == ["gemini:gemini-2.5-pro", "claude-opus-4-8"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_failover_integration.py -k node_style -q`
Expected: FAIL — `Configuration.model_chain` exists (Task 4) but until the wiring lands the test still proves the node-shape contract the rest of this task implements; it passes once Task 4 is in. (If run before Task 4, it fails with `AttributeError: model_chain`.) Its real purpose is to lock the exact key shape steps 3b–3d must produce.

- [ ] **Step 3: Write minimal implementation**

(3a) Install a fresh tracker at the run's entry node. Find `preallocate_run` (the START node, `deep_researcher_builder.add_edge(START, "preallocate_run")` at line 1663) and add the reset as its first statement. Add the import at the top of the file alongside the other `open_deep_research` imports:

```python
from open_deep_research.failover import new_run_tracker
```

and at the start of the `preallocate_run` function body:

```python
    new_run_tracker()  # fresh per-run failover state (down-set + recorded failovers)
```

(3b) Thread the chain into each stage config. For every `*_model_config`/inline `with_config({...})` model dict, add `"model_chain"` and `"stage"`. Edit each dict **in place** — add only the two new keys, never touch the existing `"model"`, `"max_tokens"`, `"api_key"`, or `"tags"` entries (this guarantees no token-cap or routing regression). The dicts and their roles:

- `clarify_with_user` `model_config` (~line 149) — role `supervisor`:
  add `"model_chain": configurable.model_chain("supervisor"),` and `"stage": "supervisor",`
- `write_research_brief` `with_config({...})` (~line 233) — role `supervisor`:
  add `"model_chain": configurable.model_chain("supervisor"), "stage": "supervisor",`
- final-report-related `with_config` (~line 268) — role `final_report`:
  add `"model_chain": configurable.model_chain("final_report"), "stage": "final_report",`
- supervisor brief model (~line 337) — role `supervisor`:
  add `"model_chain": configurable.model_chain("supervisor"), "stage": "supervisor",`
- `supervisor_model_config` (~line 416) — role `supervisor`:
  add `"model_chain": configurable.model_chain("supervisor"), "stage": "supervisor",`
- `researcher_model_config` (~line 711) — role `researcher`:
  add `"model_chain": configurable.model_chain("researcher"), "stage": "researcher",`
- `synthesizer_model` `with_config` (~line 877) — role `researcher` (compression/synthesis uses the researcher/compression model; use `compression`):
  add `"model_chain": configurable.model_chain("compression"), "stage": "compression",`
- `writer_model_config` (~line 982) — role `final_report`:
  add `"model_chain": configurable.model_chain("final_report"), "stage": "final_report",`
- summarization `with_config` (~line 1066) — role `summarization`:
  add `"model_chain": configurable.model_chain("summarization"), "stage": "summarization",`
- compression `with_config` (~line 1094) — role `compression`:
  add `"model_chain": configurable.model_chain("compression"), "stage": "compression",`
- extract_facts model (~line 1324) — role `researcher`, step `extract_facts`:
  add `"model_chain": configurable.model_chain("researcher", "extract_facts"), "stage": "extract_facts",`
- `polish_model` `with_config` (~line 1605) — role `facts_answer_polish`:
  add `"model_chain": configurable.model_chain("facts_answer_polish"), "stage": "facts_answer_polish",`

> For each site: locate the dict literal that already sets `"model": configurable.<role>_model` (or, for extract_facts, `configurable.model_for("extract_facts", "researcher")`), and add the two keys inside the same dict. The line numbers are approximate — match on the existing `"model":` entry, not the line number.

(3c) Attach failovers to the persisted run config. In `persist_research`, after `config_used.pop("mcp_config", None)` (line 1161), add:

```python
    from open_deep_research.failover import get_tracker
    config_used["failovers"] = [f.as_dict() for f in get_tracker().failovers]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_failover_integration.py -k node_style -q`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS — all prior tests plus the new failover suites. (If `ruff`/`mypy` are part of the gate: `uv run ruff check src/open_deep_research/failover.py src/open_deep_research/model_routing.py src/open_deep_research/configuration.py` clean.)

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/deep_researcher.py tests/test_failover_integration.py
git commit -m "feat(failover): wire chains + run tracker + failover record into the graph"
```

---

## Verification (whole feature)

Map to the spec's user stories:

- **US-1 mid-run quota death / US-4 cross-backend** → `test_hard_error_fails_over_to_backup` (Gemini primary hard-fails → Claude backup constructed and used).
- **US-2 sticky** → `test_sticky_skips_dead_primary_on_second_call`.
- **US-3 transient ≠ down** → `test_transient_does_not_mark_down`.
- **US-5 visibility** → failover `logger.warning` in Task 5 + `config_used["failovers"]` in Task 6 (asserted via `tracker.failovers` in the sticky test; the persisted copy rides the run's `config` JSON).
- **US-6 back-compat** → existing `tests/test_model_routing_*.py` stay green (Task 3 step 4) + `test_single_model_chain_has_no_failover_and_raises`.
- **US-7 exhausted chain** → `test_exhausted_chain_raises_last_error`.
- **Classifier / tracker / resolver units** → `tests/test_failover.py` (Tasks 1–4).

Final gate: `uv run pytest -q` all green.

## Follow-up (out of this plan)

- **Author real backup chains** into the shipped `model_routing.json` presets (e.g. `"supervisor": ["gemini:gemini-2.5-pro", "claude-opus-4-8"]`). That is a config/policy decision tied to the earlier "bang for buck" recommendation; ship it as a separate, reviewed change once these mechanics are merged.
- **Open questions for the `*.feedback` round** (already in the spec): shared-vs-per-task tracker under the researcher fan-out; combined retry-attempt bound across `_run_with_retry` × chain length; validating the `429` split against each CLI's real error strings; whether `str | list[str]` or a separate `fallbacks` map is the cleaner authoring surface.
