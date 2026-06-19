import asyncio

import pytest

from open_deep_research import claude_agent_chat as cac
from open_deep_research.claude_agent_chat import configurable_claude_model
from open_deep_research.failover import new_run_tracker


@pytest.fixture(autouse=True)
def _disable_health_file(monkeypatch):
    """Disable the health file for all integration tests to avoid cross-test pollution."""
    monkeypatch.setenv("ODR_BACKEND_HEALTH", "off")


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
    assert len(tracker.failovers) == 1  # a transient that survived backend retries is still recorded


def test_circuit_breaker_marks_down_after_repeated_transient(monkeypatch):
    # A model that throttles (429) repeatedly within a run is a circuit-breaker case: after
    # MODEL_FAILOVER_TRANSIENT_STRIKES consecutive transient failures it is marked down for the
    # run, so later calls skip it (and its retry-before-failover cost) and go straight to backup.
    monkeypatch.setenv("MODEL_FAILOVER_TRANSIENT_STRIKES", "2")
    tracker = new_run_tracker()
    constructed = []
    script = {
        "nvidia:minimaxai/minimax-m3": Exception(
            "Error code: 429 - {'status': 429, 'title': 'Too Many Requests'}"),
        "gemini:gemini-2.5-pro": "OK",
    }
    _patch_build(monkeypatch, script, constructed)
    cfg = {"model_chain": ["nvidia:minimaxai/minimax-m3", "gemini:gemini-2.5-pro"],
           "stage": "extract_facts"}
    model = configurable_claude_model().with_config(cfg)

    # Call 1: 429 -> strike 1 (< limit) -> fail over to gemini; primary NOT yet down.
    assert asyncio.run(model.ainvoke("a")) == "OK"
    assert not tracker.is_down("nvidia:minimaxai/minimax-m3")
    # Call 2: 429 -> strike 2 (== limit) -> circuit-break: primary marked down for the run.
    assert asyncio.run(model.ainvoke("b")) == "OK"
    assert tracker.is_down("nvidia:minimaxai/minimax-m3")
    # Call 3: primary skipped entirely -> straight to backup, no wasted retry on the dead model.
    constructed.clear()
    assert asyncio.run(model.ainvoke("c")) == "OK"
    assert constructed == ["gemini:gemini-2.5-pro"]


def test_circuit_breaker_strikes_reset_on_success(monkeypatch):
    # A success between blips resets the counter so transient noise never trips the breaker.
    monkeypatch.setenv("MODEL_FAILOVER_TRANSIENT_STRIKES", "2")
    tracker = new_run_tracker()
    constructed = []
    outcomes = {"n": 0}

    def fake_build(model_string, max_tokens=None):
        constructed.append(model_string)
        return _FlakyModel(model_string, outcomes)
    monkeypatch.setattr(cac, "build_chat_model", fake_build)

    cfg = {"model_chain": ["nvidia:glm", "claude-haiku-4-5"], "stage": "summarization"}
    model = configurable_claude_model().with_config(cfg)
    # primary: 429, then OK (resets), then 429 again -> only 1 strike since the reset -> not down.
    for _ in range(3):
        asyncio.run(model.ainvoke("x"))
    assert not tracker.is_down("nvidia:glm")


class _FlakyModel:
    """Primary throttles on odd calls, succeeds on even ones; backup always OK."""

    def __init__(self, model_id, state):
        self.model_id = model_id
        self.state = state

    def with_structured_output(self, *a, **k):
        return self

    def bind_tools(self, *a, **k):
        return self

    def with_retry(self, *a, **k):
        return self

    async def ainvoke(self, *a, **k):
        if self.model_id != "nvidia:glm":
            return "BACKUP-OK"
        self.state["n"] += 1
        if self.state["n"] % 2 == 1:  # 1st, 3rd call throttle; 2nd succeeds (resets strikes)
            raise Exception("Error code: 429 - Too Many Requests")
        return "PRIMARY-OK"


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


def test_no_chain_key_behaves_like_before(monkeypatch):
    """A plain {"model": ...} caller (no model_chain) keeps old behaviour: raise, no failover."""
    new_run_tracker()
    constructed = []
    script = {"claude-opus-4-8": Exception("quota exceeded")}
    _patch_build(monkeypatch, script, constructed)
    model = configurable_claude_model().with_config({"model": "claude-opus-4-8"})
    with pytest.raises(Exception, match="quota exceeded"):
        asyncio.run(model.ainvoke("x"))
    assert constructed == ["claude-opus-4-8"]  # single model, tried once, no failover


def test_no_chain_key_success(monkeypatch):
    """A plain {"model": ...} caller returns the model result unchanged on success."""
    new_run_tracker()
    constructed = []
    script = {"claude-opus-4-8": "PLAIN-OK"}
    _patch_build(monkeypatch, script, constructed)
    model = configurable_claude_model().with_config({"model": "claude-opus-4-8"})
    assert asyncio.run(model.ainvoke("x")) == "PLAIN-OK"
    assert constructed == ["claude-opus-4-8"]


def test_node_style_config_shape_drives_failover(monkeypatch, tmp_path):
    """A config dict built the way a graph node builds it (model + model_chain +
    stage, sourced from a Configuration) actually engages reactive failover."""
    import json

    from open_deep_research.configuration import Configuration

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
    # exactly the keys a node attaches (see step 3b)
    model = configurable_claude_model().with_config({
        "model": c.supervisor_model,
        "model_chain": c.model_chain("supervisor"),
        "stage": "supervisor",
    })
    assert asyncio.run(model.ainvoke("hi")) == "BACKUP-OK"
    assert constructed == ["gemini:gemini-2.5-pro", "claude-opus-4-8"]


def test_inflight_peer_skips_just_marked_down_model(monkeypatch):
    # Two concurrent calls share a tracker. The first marks the primary backend down
    # (backend_fatal); the second, started after, must skip the primary entirely.
    tracker = new_run_tracker()
    constructed = []
    script = {
        "gemini:gemini-2.5-pro": Exception("429 insufficient_quota"),  # backend_fatal -> mark down
        "claude-opus-4-8": "OK",
    }
    _patch_build(monkeypatch, script, constructed)
    cfg = {"model_chain": ["gemini:gemini-2.5-pro", "claude-opus-4-8"], "stage": "extract_facts"}
    model = configurable_claude_model().with_config(cfg)
    asyncio.run(model.ainvoke("first"))   # marks gemini backend down
    constructed.clear()
    asyncio.run(model.ainvoke("second"))  # must skip gemini
    assert constructed == ["claude-opus-4-8"]


def test_per_attempt_skip_within_available(monkeypatch):
    # available_chain is computed once; ensure the loop re-checks is_down before each attempt so a
    # model marked down by a peer between chain-build and this attempt is not tried.
    tracker = new_run_tracker()
    constructed = []

    def fake_build(model_string, max_tokens=None):
        constructed.append(model_string)
        # mark the primary down right after the chain was built but before we try it
        if model_string == "nvidia:x":
            tracker.mark_backend_down("nvidia")
        return _FakeModel(model_string, {"nvidia:x": Exception("429"), "claude-opus-4-8": "OK"})

    monkeypatch.setattr(cac, "build_chat_model", fake_build)
    model = configurable_claude_model().with_config(
        {"model_chain": ["nvidia:x", "claude-opus-4-8"], "stage": "extract_facts"})
    assert asyncio.run(model.ainvoke("x")) == "OK"
