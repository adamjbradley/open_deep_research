import asyncio

import pytest

from open_deep_research.failover import (
    AvailabilityTracker,
    FailoverRecord,
    classify_error,
    get_tracker,
    new_run_tracker,
    reason_for,
)
from open_deep_research.model_routing import (
    model_chain,
    resolve_model,
    routing_from_dict,
)


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


def test_classify_error_empty_message():
    assert classify_error(Exception("")) == "transient"


def test_reason_for_empty_message():
    assert reason_for(Exception(""), "hard") == "hard: Exception"


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
    import contextvars

    from open_deep_research.failover import _current_tracker

    def _in_fresh_context():
        _current_tracker.set(None)          # guarantee no prior tracker
        t = get_tracker()                   # should lazy-create
        assert isinstance(t, AvailabilityTracker)
        assert get_tracker() is t           # stable within the context
        return "ok"

    ctx = contextvars.copy_context()
    assert ctx.run(_in_fresh_context) == "ok"


def test_available_chain_all_down_returns_empty():
    t = AvailabilityTracker()
    chain = ["model-a", "model-b"]
    t.mark_down("model-a")
    t.mark_down("model-b")
    assert t.available_chain(chain) == []


# ---------------------------------------------------------------------------
# Task 3: chain-aware routing resolution
# ---------------------------------------------------------------------------

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


def test_model_for_list_step_override_returns_head(monkeypatch, tmp_path):
    """model_for's contract is a single string; a list step override returns its head."""
    import json

    from open_deep_research.configuration import Configuration

    f = tmp_path / "model_routing.json"
    f.write_text(json.dumps({
        "version": "1", "active_preset": "p",
        "presets": {"p": {
            "roles": {"researcher": "gemini:gemini-2.5-flash"},
            "step_overrides": {"extract_facts": ["claude:sonnet", "gemini:gemini-2.5-flash"]},
        }},
    }), encoding="utf-8")
    monkeypatch.setenv("MODEL_ROUTING_FILE", str(f))
    for k in ("MODEL_ROUTING_PRESET", "RESEARCHER_MODEL"):
        monkeypatch.delenv(k, raising=False)
    c = Configuration.from_runnable_config({})
    assert c.model_for("extract_facts", "researcher") == "claude:sonnet"   # head, not the list


def test_configuration_model_chain_uses_preset_list(monkeypatch, tmp_path):
    import json

    from open_deep_research.configuration import Configuration

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
    import json

    from open_deep_research.configuration import Configuration

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
    import json

    from open_deep_research.configuration import Configuration

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
