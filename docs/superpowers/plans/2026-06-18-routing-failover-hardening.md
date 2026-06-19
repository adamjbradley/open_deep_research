# Routing Failover Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the existing model-failover path so the `gemini` routing preset cannot silently drain an exhausted Claude, fails loud when a backend is unusable, and remembers exhaustion across runs.

**Architecture:** Extend the existing run-scoped `AvailabilityTracker` (failover.py) from per-model to per-backend down-tracking, persist backend exhaustion to a TTL'd health file read at run start, add a run-start preflight that probes the active preset's primary backend, and add two loud-failure guards (empty-notes in deep_researcher.py, sync-path in claude_agent_chat.py).

**Tech Stack:** Python 3.11, pydantic v2, pytest, asyncio, LangGraph. Models invoked via `configurable_claude_model` (claude_agent_chat.py) which routes through CLI backends (claude/gemini/codex).

## Global Constraints

- Routing-as-data unchanged: no edits to preset definitions or the `model_routing.json` schema.
- Transient retry/backoff behaviour unchanged — this work only touches hard-failure classification and chain selection.
- Health-file and preflight code is best-effort: a corrupt/locked file or a probe error must NEVER abort a run (except G3 `fail` policy, which raises by design).
- Tests live in `tests/` as `test_*.py`; run with `python -m pytest`. Match existing `test_failover.py` style.
- Default behaviour must not change for users who never set the new env vars, except: an exhausted backend is now skipped chain-wide (the intended fix).

---

### Task 1: Backend-level mark-down (G1)

Split hard errors into *backend-fatal* (kill the whole backend for the run) vs *model-fatal* (kill just that model), and teach `AvailabilityTracker` to track downed backends.

**Files:**
- Modify: `src/open_deep_research/failover.py`
- Modify: `src/open_deep_research/claude_agent_chat.py:1215-1226` (the `ainvoke` except block)
- Test: `tests/test_failover.py`

**Interfaces:**
- Produces: `backend_of(model: str) -> str`; `classify_error(exc) -> str` now returns one of `"backend_fatal" | "model_fatal" | "transient"`; `AvailabilityTracker.mark_backend_down(backend: str)`, `AvailabilityTracker.is_backend_down(backend: str) -> bool`; `available_chain` excludes models whose backend is down.
- Consumes (claude_agent_chat.py): the three-way `classify_error` return.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_failover.py  (add)
from open_deep_research.failover import (
    AvailabilityTracker, backend_of, classify_error,
)

def test_backend_of_derives_backend():
    assert backend_of("gemini:gemini-2.5-flash") == "gemini"
    assert backend_of("codex:gpt-5.5") == "codex"
    assert backend_of("claude-opus-4-8") == "claude"
    assert backend_of("claude:opus") == "claude"

def test_classify_three_way():
    assert classify_error(Exception("429 insufficient_quota")) == "backend_fatal"
    assert classify_error(Exception("401 unauthorized")) == "backend_fatal"
    assert classify_error(Exception("model not found: foo")) == "model_fatal"
    assert classify_error(Exception("404")) == "model_fatal"
    assert classify_error(Exception("overloaded, try again")) == "transient"
    assert classify_error(TimeoutError()) == "transient"

def test_backend_down_skips_all_models_of_backend():
    t = AvailabilityTracker()
    t.mark_backend_down("claude")
    chain = ["gemini:gemini-2.5-flash", "claude-opus-4-6", "claude-opus-4-8"]
    assert t.available_chain(chain) == ["gemini:gemini-2.5-flash"]

def test_model_fatal_does_not_kill_backend():
    t = AvailabilityTracker()
    t.mark_down("claude-opus-4-6")
    assert t.is_backend_down("claude") is False
    assert t.available_chain(["claude-opus-4-6", "claude-opus-4-8"]) == ["claude-opus-4-8"]
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python -m pytest tests/test_failover.py -k "backend or three_way" -v`
Expected: FAIL — `ImportError: cannot import name 'backend_of'` / new return values mismatch.

- [ ] **Step 3: Implement in failover.py**

Replace `_HARD_MARKERS` with two sets and update `classify_error`; add `backend_of`; extend `AvailabilityTracker`.

```python
# Backend-fatal: the whole backend is unusable this run (quota/billing/auth).
_BACKEND_FATAL_MARKERS = (
    "quota", "insufficient_quota", "resource_exhausted", "billing",
    "unauthorized", "invalid api key", "invalid_api_key",
    "permission denied", "401", "403",
)
# Model-fatal: only this model id is bad (wrong/removed name).
_MODEL_FATAL_MARKERS = (
    "model not found", "model_not_found", "does not exist", "404",
)
# _TRANSIENT_MARKERS: unchanged.

_KNOWN_PREFIXES = {"gemini", "google", "codex", "openai", "claude", "anthropic"}

def backend_of(model: str) -> str:
    """The backend a model spec runs on: the ':' prefix, else 'claude' for bare claude ids."""
    head = model.split(":", 1)[0].strip().lower() if ":" in model else ""
    if head in _KNOWN_PREFIXES:
        return "google" if head == "google" else ("anthropic" if head == "anthropic" else head)
    return "claude"

def classify_error(exc: BaseException) -> str:
    """'backend_fatal' | 'model_fatal' | 'transient' (see module docstring)."""
    if isinstance(exc, asyncio.TimeoutError | TimeoutError):
        return "transient"
    text = str(exc).lower()
    if any(m in text for m in _BACKEND_FATAL_MARKERS):
        return "backend_fatal"
    if any(m in text for m in _MODEL_FATAL_MARKERS):
        return "model_fatal"
    if any(m in text for m in _TRANSIENT_MARKERS):
        return "transient"
    return "transient"
```

Extend the dataclass:

```python
@dataclass
class AvailabilityTracker:
    _down: set[str] = field(default_factory=set)
    _down_backends: set[str] = field(default_factory=set)
    failovers: list[FailoverRecord] = field(default_factory=list)

    def is_down(self, model: str) -> bool:
        return model in self._down or backend_of(model) in self._down_backends

    def mark_down(self, model: str) -> None:
        self._down.add(model)

    def is_backend_down(self, backend: str) -> bool:
        return backend in self._down_backends

    def mark_backend_down(self, backend: str) -> None:
        self._down_backends.add(backend)

    def available_chain(self, chain: list[str]) -> list[str]:
        return [m for m in chain if not self.is_down(m)]

    def record_failover(self, stage, from_model, to_model, reason) -> None:
        self.failovers.append(FailoverRecord(stage, from_model, to_model, reason))
```

- [ ] **Step 4: Update the `ainvoke` except block in claude_agent_chat.py**

```python
            except Exception as exc:  # noqa: BLE001 - re-raised below when the chain is exhausted
                last_exc = exc
                kind = classify_error(exc)
                if kind == "backend_fatal":
                    tracker.mark_backend_down(backend_of(model_string))
                elif kind == "model_fatal":
                    tracker.mark_down(model_string)
                if idx >= len(available) - 1:
                    raise
                next_model = available[idx + 1]
                reason = reason_for(exc, kind)
                tracker.record_failover(stage, model_string, next_model, reason)
                logger.warning("failover[%s]: %s unavailable (%s) -> %s",
                               stage, model_string, reason, next_model)
```

Add `backend_of` to the import on line 1190: `from open_deep_research.failover import backend_of, classify_error, get_tracker, reason_for`.

- [ ] **Step 5: Run tests, verify pass**

Run: `python -m pytest tests/test_failover.py -v`
Expected: PASS (all, including pre-existing tests — confirm none referenced the removed `_HARD_MARKERS` name; if a test imported it, update it to the new sets).

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/failover.py src/open_deep_research/claude_agent_chat.py tests/test_failover.py
git commit -m "feat(failover): backend-level mark-down for backend-fatal errors (G1)"
```

---

### Task 2: Gemini auth-string coverage (G4)

A logged-out gemini CLI must classify as backend-fatal so it fails over instead of burning transient retries.

**Files:**
- Modify: `src/open_deep_research/failover.py` (`_BACKEND_FATAL_MARKERS`)
- Test: `tests/test_failover.py`

**Interfaces:**
- Consumes: `classify_error` from Task 1.

- [ ] **Step 1: Confirm the real logged-out CLI error text**

Run: `gemini --help 2>&1 | head -5` then, if safe, observe the error text a logged-out call emits (do NOT log out a working session; instead inspect docs/strings). Record the exact phrase. If it cannot be confirmed live, use the defensive superset below.

- [ ] **Step 2: Write failing test**

```python
# tests/test_failover.py  (add)
import pytest
from open_deep_research.failover import classify_error

@pytest.mark.parametrize("msg", [
    "Please run gemini auth login: not logged in",
    "Error: not authenticated",
    "no credentials found, please authenticate",
    "reauthenticate to continue",
])
def test_gemini_logged_out_is_backend_fatal(msg):
    assert classify_error(Exception(msg)) == "backend_fatal"
```

- [ ] **Step 3: Run test, verify it fails**

Run: `python -m pytest tests/test_failover.py -k gemini_logged_out -v`
Expected: FAIL — these strings currently classify `transient`.

- [ ] **Step 4: Extend `_BACKEND_FATAL_MARKERS`**

```python
_BACKEND_FATAL_MARKERS = (
    "quota", "insufficient_quota", "resource_exhausted", "billing",
    "unauthorized", "invalid api key", "invalid_api_key",
    "permission denied", "401", "403",
    # gemini/codex CLI logged-out / credential-missing surfaces:
    "not logged in", "not authenticated", "no credentials",
    "please authenticate", "reauthenticate",
)
```

- [ ] **Step 5: Run test, verify pass**

Run: `python -m pytest tests/test_failover.py -k gemini_logged_out -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/failover.py tests/test_failover.py
git commit -m "feat(failover): classify logged-out gemini CLI as backend-fatal (G4)"
```

---

### Task 3: Cross-run backend health file (G2)

Persist backend-fatal exhaustion to a TTL'd file so new runs skip the dead backend instead of re-paying its failed first call.

**Files:**
- Modify: `src/open_deep_research/failover.py`
- Test: `tests/test_failover.py`

**Interfaces:**
- Produces: `record_backend_exhausted(backend: str, *, now: float | None = None) -> None`; `load_exhausted_backends(*, now: float | None = None) -> set[str]`; `new_run_tracker` seeds `_down_backends` from `load_exhausted_backends()`.
- Consumes: `mark_backend_down`, `backend_of` from Task 1.

Env contract: `ODR_BACKEND_HEALTH=off` disables; `ODR_BACKEND_HEALTH_TTL` seconds (default `900`); `ODR_BACKEND_HEALTH_FILE` overrides the path (default `<platform-cache>/odr/backend_health.json`, computed without extra deps via `os.path.expanduser` + `LOCALAPPDATA`/`XDG_CACHE_HOME` fallback).

- [ ] **Step 1: Write failing tests (use `tmp_path` + `now=` injection — no real clock)**

```python
# tests/test_failover.py  (add)
import json
from open_deep_research import failover as fo

def test_health_file_roundtrip_and_ttl(tmp_path, monkeypatch):
    f = tmp_path / "backend_health.json"
    monkeypatch.setenv("ODR_BACKEND_HEALTH_FILE", str(f))
    monkeypatch.setenv("ODR_BACKEND_HEALTH_TTL", "100")
    fo.record_backend_exhausted("claude", now=1000.0)
    assert fo.load_exhausted_backends(now=1050.0) == {"claude"}   # within TTL
    assert fo.load_exhausted_backends(now=1200.0) == set()        # past TTL

def test_health_off_disables(tmp_path, monkeypatch):
    f = tmp_path / "backend_health.json"
    monkeypatch.setenv("ODR_BACKEND_HEALTH_FILE", str(f))
    monkeypatch.setenv("ODR_BACKEND_HEALTH", "off")
    fo.record_backend_exhausted("claude", now=1000.0)
    assert not f.exists()
    assert fo.load_exhausted_backends(now=1000.0) == set()

def test_corrupt_health_file_is_ignored(tmp_path, monkeypatch):
    f = tmp_path / "backend_health.json"; f.write_text("{ not json")
    monkeypatch.setenv("ODR_BACKEND_HEALTH_FILE", str(f))
    assert fo.load_exhausted_backends(now=1000.0) == set()

def test_new_run_tracker_seeds_downed_backends(tmp_path, monkeypatch):
    f = tmp_path / "backend_health.json"
    monkeypatch.setenv("ODR_BACKEND_HEALTH_FILE", str(f))
    monkeypatch.setenv("ODR_BACKEND_HEALTH_TTL", "100")
    fo.record_backend_exhausted("claude", now=1000.0)
    t = fo.new_run_tracker("thread-x", now=1050.0)
    assert t.is_backend_down("claude") is True
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python -m pytest tests/test_failover.py -k "health or seeds" -v`
Expected: FAIL — functions undefined; `new_run_tracker` has no `now` kwarg.

- [ ] **Step 3: Implement in failover.py**

```python
import json, os, time

def _health_enabled() -> bool:
    return os.environ.get("ODR_BACKEND_HEALTH", "").strip().lower() != "off"

def _health_ttl() -> float:
    try:
        return float(os.environ.get("ODR_BACKEND_HEALTH_TTL", "900"))
    except ValueError:
        return 900.0

def _health_path() -> str:
    p = os.environ.get("ODR_BACKEND_HEALTH_FILE")
    if p:
        return p
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") \
        or os.path.expanduser("~/.cache")
    return os.path.join(base, "odr", "backend_health.json")

def load_exhausted_backends(*, now: float | None = None) -> set[str]:
    """Backends whose persisted exhaustion has not yet expired. Best-effort; never raises."""
    if not _health_enabled():
        return set()
    now = time.time() if now is None else now
    try:
        with open(_health_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return {b for b, until in data.items() if isinstance(until, (int, float)) and until > now}
    except Exception:  # noqa: BLE001 - missing/corrupt/locked file is non-fatal
        return set()

def record_backend_exhausted(backend: str, *, now: float | None = None) -> None:
    """Persist `backend` as exhausted-until now+TTL. Best-effort; never raises."""
    if not _health_enabled():
        return
    now = time.time() if now is None else now
    path = _health_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                data = {}
        except Exception:  # noqa: BLE001
            data = {}
        data[backend] = now + _health_ttl()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception:  # noqa: BLE001 - persistence is best-effort
        pass
```

Update `new_run_tracker` to seed from the file:

```python
def new_run_tracker(key: str | None = None, *, now: float | None = None) -> AvailabilityTracker:
    t = AvailabilityTracker()
    for b in load_exhausted_backends(now=now):
        t.mark_backend_down(b)
    if key is not None:
        _registry[key] = t
    _current_tracker.set(t)
    return t
```

- [ ] **Step 4: Persist on backend-fatal mark-down (claude_agent_chat.py `ainvoke`)**

In the except block from Task 1, after `tracker.mark_backend_down(...)`:

```python
                if kind == "backend_fatal":
                    bk = backend_of(model_string)
                    tracker.mark_backend_down(bk)
                    from open_deep_research.failover import record_backend_exhausted
                    record_backend_exhausted(bk)
```

- [ ] **Step 5: Run tests, verify pass**

Run: `python -m pytest tests/test_failover.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/failover.py src/open_deep_research/claude_agent_chat.py tests/test_failover.py
git commit -m "feat(failover): persist backend exhaustion to TTL health file (G2)"
```

---

### Task 4: Run-start preflight (G3)

Probe the active preset's primary backend(s) once per process; warn-and-mark-down or fail loud per policy.

**Files:**
- Create: `src/open_deep_research/preflight.py`
- Modify: `src/open_deep_research/deep_researcher.py` (at the `new_run_tracker(thread_id)` call, ~line 1481)
- Test: `tests/test_preflight.py`

**Interfaces:**
- Produces: `primary_backends(preset) -> set[str]`; `probe_backend(backend: str) -> bool` (True = usable); `run_preflight(routing, tracker, *, policy: str | None = None) -> list[str]` (returns unusable primaries; raises `PreflightError` under `fail`).
- Consumes: `RoutingConfig`/`Preset` from `model_routing`, `backend_of` + `AvailabilityTracker` from failover.

Policy env: `ODR_PREFLIGHT` ∈ `warn` (default) | `fail` | `off`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_preflight.py  (new)
import pytest
from open_deep_research.model_routing import routing_from_dict
from open_deep_research.failover import AvailabilityTracker
from open_deep_research import preflight as pf

ROUTING = {
    "version": "1", "active_preset": "gemini",
    "presets": {"gemini": {"roles": {
        "supervisor": ["gemini:gemini-2.5-flash", "claude-opus-4-8"],
        "researcher": ["gemini:gemini-2.5-flash", "claude-opus-4-6"],
    }, "search": "tavily"}},
}

def test_primary_backends_are_chain_heads():
    r = routing_from_dict(ROUTING)
    assert pf.primary_backends(r.active()) == {"gemini"}

def test_warn_marks_unusable_primary_down(monkeypatch):
    monkeypatch.setenv("ODR_PREFLIGHT", "warn")
    monkeypatch.setattr(pf, "probe_backend", lambda b: False)
    r = routing_from_dict(ROUTING); t = AvailabilityTracker()
    unusable = pf.run_preflight(r, t)
    assert unusable == ["gemini"]
    assert t.is_backend_down("gemini") is True

def test_fail_raises(monkeypatch):
    monkeypatch.setenv("ODR_PREFLIGHT", "fail")
    monkeypatch.setattr(pf, "probe_backend", lambda b: False)
    r = routing_from_dict(ROUTING); t = AvailabilityTracker()
    with pytest.raises(pf.PreflightError):
        pf.run_preflight(r, t)

def test_off_skips(monkeypatch):
    monkeypatch.setenv("ODR_PREFLIGHT", "off")
    called = []
    monkeypatch.setattr(pf, "probe_backend", lambda b: called.append(b) or True)
    r = routing_from_dict(ROUTING); t = AvailabilityTracker()
    assert pf.run_preflight(r, t) == []
    assert called == []

def test_probe_is_memoized(monkeypatch):
    calls = []
    monkeypatch.setattr(pf, "_probe_uncached", lambda b: calls.append(b) or True)
    pf._probe_cache.clear()
    assert pf.probe_backend("gemini") is True
    assert pf.probe_backend("gemini") is True
    assert calls == ["gemini"]
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python -m pytest tests/test_preflight.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement preflight.py**

```python
"""Run-start preflight: probe the active preset's primary backends before work begins."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)


class PreflightError(RuntimeError):
    """Raised under ODR_PREFLIGHT=fail when a primary backend is unusable."""


def primary_backends(preset) -> set[str]:
    from open_deep_research.failover import backend_of
    heads = set()
    for spec in preset.roles.values():
        chain = [spec] if isinstance(spec, str) else list(spec)
        if chain:
            heads.add(backend_of(chain[0]))
    return heads


def _probe_uncached(backend: str) -> bool:
    """True if the backend looks usable. Claude (subscription) always True; gemini/codex probed via CLI."""
    if backend in ("claude", "anthropic"):
        return True
    if backend in ("gemini", "google"):
        binname = os.environ.get("GEMINI_CLI_BIN", "gemini")
        if shutil.which(binname) is None:
            return False
        try:
            # Cheap, non-interactive: a version/help call exits 0 only when the CLI is runnable.
            r = subprocess.run([binname, "--version"], capture_output=True, timeout=15)
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False
    if backend == "codex":
        return shutil.which(os.environ.get("CODEX_CLI_BIN", "codex")) is not None
    return True


_probe_cache: dict[str, bool] = {}


def probe_backend(backend: str) -> bool:
    if backend not in _probe_cache:
        _probe_cache[backend] = _probe_uncached(backend)
    return _probe_cache[backend]


def run_preflight(routing, tracker, *, policy: str | None = None) -> list[str]:
    policy = (policy or os.environ.get("ODR_PREFLIGHT", "warn")).strip().lower()
    if policy == "off":
        return []
    preset = routing.active()
    unusable = sorted(b for b in primary_backends(preset) if not probe_backend(b))
    if not unusable:
        return []
    msg = (f"preflight: active preset primary backend(s) {unusable} not usable "
           f"(e.g. gemini CLI not logged in -> run `gemini auth login`, or set MODEL_ROUTING_PRESET)")
    if policy == "fail":
        raise PreflightError(msg)
    logger.warning("%s; marking down so the run uses backups", msg)
    for b in unusable:
        tracker.mark_backend_down(b)
    return unusable
```

- [ ] **Step 4: Wire into deep_researcher.py run start**

At the `new_run_tracker(thread_id)` site (~line 1481), capture the tracker and run preflight:

```python
    tracker = new_run_tracker(thread_id)  # fresh per-run failover state keyed by thread_id
    try:
        from open_deep_research.preflight import run_preflight
        from open_deep_research.model_routing import load_routing
        run_preflight(load_routing(), tracker)
    except Exception as e:  # PreflightError (fail policy) or unexpected probe error
        from open_deep_research.preflight import PreflightError
        if isinstance(e, PreflightError):
            raise
        logger.warning("preflight skipped due to probe error: %s", e)
```

- [ ] **Step 5: Run tests, verify pass**

Run: `python -m pytest tests/test_preflight.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/preflight.py src/open_deep_research/deep_researcher.py tests/test_preflight.py
git commit -m "feat(preflight): probe active preset primary backend at run start (G3)"
```

---

### Task 5: Empty-notes guard (G5)

When every dispatched research unit fails and no notes survive, surface a loud, persisted failure instead of synthesizing a report from nothing.

**Files:**
- Modify: `src/open_deep_research/deep_researcher.py` (sentinels block ~line 88; aggregation block ~line 645-667)
- Test: `tests/test_failover_graph_integration.py`

**Interfaces:**
- Produces: `ALL_RESEARCH_FAILED_SENTINEL` (module constant); `_report_is_failed` recognises it.
- Consumes: the per-unit `tool_results` list and `all_tool_messages` from the supervisor gather.

- [ ] **Step 1: Write failing test**

```python
# tests/test_failover_graph_integration.py  (add)
from open_deep_research.deep_researcher import (
    ALL_RESEARCH_FAILED_SENTINEL, _report_is_failed,
)

def test_all_research_failed_sentinel_is_a_failed_report():
    assert _report_is_failed(ALL_RESEARCH_FAILED_SENTINEL) is True

def test_partial_success_is_not_failed():
    assert _report_is_failed("Real findings about India digital ID...") is False
```

- [ ] **Step 2: Run test, verify it fails**

Run: `python -m pytest tests/test_failover_graph_integration.py -k all_research_failed -v`
Expected: FAIL — `ImportError: cannot import name 'ALL_RESEARCH_FAILED_SENTINEL'`.

- [ ] **Step 3: Add the sentinel and detection**

Near line 88:

```python
ALL_RESEARCH_FAILED_SENTINEL = (
    "Error: all research units failed (no usable findings). "
    "Likely all model backends are unavailable (quota/auth). See run failovers."
)
```

Extend `_report_is_failed` (line 92-97):

```python
def _report_is_failed(report: Optional[str]) -> bool:
    if not report or not report.strip():
        return True
    stripped = report.strip()
    return (stripped.startswith(REPORT_FAILED_PREFIX)
            or stripped == COMPRESSION_FAILED_SENTINEL
            or stripped == ALL_RESEARCH_FAILED_SENTINEL)
```

In the aggregation block, after computing `raw_notes_concat` (line 664) and before `if raw_notes_concat:`:

```python
            allowed_n = len(allowed_conduct_research_calls)
            all_failed = allowed_n > 0 and all(
                isinstance(o, BaseException) for o in tool_results
            )
            if all_failed and not raw_notes_concat:
                from open_deep_research.failover import get_tracker
                fos = get_tracker(state.get("thread_id") if isinstance(state, dict) else None).failovers
                logger.error("All %d research units failed and produced no notes; "
                             "failovers=%s", allowed_n, [f.as_dict() for f in fos])
                update_payload["raw_notes"] = [ALL_RESEARCH_FAILED_SENTINEL]
```

(Using the sentinel — caught by `_report_is_failed` — keeps the supervisor's react loop intact while ensuring persistence never saves the run as completed or merges it into the dossier, matching the existing COMPRESSION sentinel contract.)

- [ ] **Step 4: Run test, verify pass**

Run: `python -m pytest tests/test_failover_graph_integration.py -k "all_research_failed or partial_success" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/deep_researcher.py tests/test_failover_graph_integration.py
git commit -m "feat(failover): loud sentinel when all research units fail (G5)"
```

---

### Task 6: Sync-path failover guard (G6)

`invoke()` (sync) has no failover; ensure a multi-element chain can never silently run only its head.

**Files:**
- Modify: `src/open_deep_research/claude_agent_chat.py:1230-1231` (`invoke`)
- Test: `tests/test_failover.py`

**Interfaces:**
- Consumes: `_resolve_chain` (existing method).

- [ ] **Step 1: Write failing test**

```python
# tests/test_failover.py  (add)
import pytest
from open_deep_research.claude_agent_chat import configurable_claude_model

def test_sync_invoke_rejects_multielement_chain():
    m = configurable_claude_model({"model_chain": ["gemini:gemini-2.5-flash", "claude-opus-4-8"],
                                    "stage": "researcher"})
    with pytest.raises(RuntimeError, match="sync invoke"):
        m.invoke("hi")
```

- [ ] **Step 2: Run test, verify it fails**

Run: `python -m pytest tests/test_failover.py -k sync_invoke -v`
Expected: FAIL — no guard; it would try to run the head (or error elsewhere).

- [ ] **Step 3: Add the guard in `invoke`**

```python
    def invoke(self, input: Any, config: Optional[RunnableConfig] = None, **kwargs: Any):
        chain, _, _ = self._resolve_chain(config)
        if len(chain) > 1:
            raise RuntimeError(
                "sync invoke() does not support model failover; use ainvoke() for a "
                f"multi-element chain ({chain})")
        return self._materialize(config).invoke(input, config, **kwargs)
```

- [ ] **Step 4: Run test, verify pass**

Run: `python -m pytest tests/test_failover.py -k sync_invoke -v`
Expected: PASS

- [ ] **Step 5: Confirm no hot role relies on sync `invoke`**

Run: `grep -rnE "\.invoke\(" src/open_deep_research/deep_researcher.py`
Expected: no graph stage uses the configurable model's sync `.invoke` for a multi-chain role (they use `ainvoke`). If one does, convert it to `ainvoke`. Record the finding in the commit body.

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/claude_agent_chat.py tests/test_failover.py
git commit -m "feat(failover): guard sync invoke() against silent no-failover (G6)"
```

---

### Task 7: Full-suite regression + docs note

**Files:**
- Modify: `.env.example` (document new env vars)
- Test: whole suite

- [ ] **Step 1: Run the full failover + routing suite**

Run: `python -m pytest tests/test_failover.py tests/test_failover_integration.py tests/test_failover_graph_integration.py tests/test_preflight.py tests/test_model_routing_resolve.py -v`
Expected: PASS

- [ ] **Step 2: Document the new env vars in `.env.example`**

```bash
# --- Failover hardening ---
# Preflight policy when the active preset's primary backend is unusable (e.g. gemini CLI
# not logged in): warn (default, run on backups) | fail (abort loudly) | off.
ODR_PREFLIGHT=warn
# Remember a backend's quota/auth exhaustion across runs for this many seconds (0/off to disable).
ODR_BACKEND_HEALTH_TTL=900
# ODR_BACKEND_HEALTH=off            # disable cross-run backend health memory entirely
# ODR_BACKEND_HEALTH_FILE=/path/to/backend_health.json   # override default cache location
```

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "docs(env): document failover-hardening env vars (ODR_PREFLIGHT, ODR_BACKEND_HEALTH*)"
```

---

## Self-Review

**Spec coverage:** G1→Task 1; G2→Task 3; G3→Task 4; G4→Task 2; G5→Task 5; G6→Task 6; observability (loud logs + `config_used["failovers"]`) covered by Tasks 1/4/5 logging. Env documentation → Task 7. All spec sections mapped.

**Placeholder scan:** No TBD/TODO. The one live-verification step (Task 2 Step 1, exact gemini logged-out string) ships a defensive superset so the task is complete even if the live string can't be confirmed — not a placeholder.

**Type consistency:** `backend_of`, `classify_error` (3-way), `mark_backend_down`/`is_backend_down`, `record_backend_exhausted`/`load_exhausted_backends`, `new_run_tracker(key, *, now=)`, `run_preflight`/`probe_backend`/`primary_backends`/`PreflightError`, `ALL_RESEARCH_FAILED_SENTINEL` — names used consistently across tasks. Task 3 Step 4 depends on `backend_of` imported in Task 1 Step 4 (same import line).

**Ordering:** Task 1 (markers/backend) precedes 2 (more markers), 3 (uses backend), 4 (uses mark_backend_down). 5 and 6 are independent. 7 is the regression gate.
