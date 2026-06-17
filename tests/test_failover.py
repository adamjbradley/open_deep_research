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
