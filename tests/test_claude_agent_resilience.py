"""Resilience tests for the Claude Agent SDK backend (timeout + transient retry).

Background: the subscription backend consumes the model/search response via
``async for msg in cas.query(...)`` with no timeout and no retry. Two real
failures came out of this:

1. A hung ``claude.exe`` subprocess never yields a result, so the consume blocks
   forever -- the un-timed ``asyncio.gather`` in the graph then freezes the whole
   run (status stays ``running`` indefinitely).
2. Under concurrent load the CLI sometimes returns a contradictory result
   envelope (``is_error: true`` with ``subtype: "success"`` and no error details,
   then a non-zero exit). The SDK surfaces this as
   ``Exception("Claude Code returned an error result: success")`` -- a transient,
   non-actionable failure that should be retried rather than propagated.

These tests are dependency-free: they exercise the classification, retry, and
timeout helpers directly with fakes -- no real CLI, SDK session, or network.
"""
import asyncio

import open_deep_research.claude_agent_chat as cac
from open_deep_research.claude_agent_chat import (
    _drain_query_with_timeout,
    _is_transient_sdk_error,
    _run_with_retry,
)


# -- classification ---------------------------------------------------------

def test_error_result_success_is_transient():
    """The contradictory CLI envelope must be treated as a retryable transient."""
    exc = Exception("Claude Code returned an error result: success")
    assert _is_transient_sdk_error(exc) is True


def test_timeout_is_transient():
    assert _is_transient_sdk_error(asyncio.TimeoutError()) is True
    assert _is_transient_sdk_error(TimeoutError()) is True


def test_overload_and_connection_are_transient():
    assert _is_transient_sdk_error(Exception("API Error: 429 overloaded_error")) is True
    assert _is_transient_sdk_error(RuntimeError("CLI failed (exit code 1): boom")) is True
    assert _is_transient_sdk_error(Exception("Connection reset by peer")) is True


def test_genuine_errors_are_not_transient():
    """Real bugs must surface immediately, not be masked by retries."""
    assert _is_transient_sdk_error(ValueError("invalid json schema for tool envelope")) is False
    assert _is_transient_sdk_error(KeyError("research_topic")) is False


# -- retry ------------------------------------------------------------------

def test_retry_recovers_after_transient_failures():
    """A transient failure that clears on retry yields the eventual success."""
    calls = {"n": 0}

    async def attempt():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Exception("Claude Code returned an error result: success")
        return "ok"

    result = asyncio.run(_run_with_retry(attempt, max_attempts=3, backoff_s=0))
    assert result == "ok"
    assert calls["n"] == 3


def test_retry_reraises_non_transient_immediately():
    """Non-transient errors are not retried -- they propagate on the first try."""
    calls = {"n": 0}

    async def attempt():
        calls["n"] += 1
        raise ValueError("schema mismatch")

    try:
        asyncio.run(_run_with_retry(attempt, max_attempts=5, backoff_s=0))
        assert False, "expected ValueError to propagate"
    except ValueError:
        pass
    assert calls["n"] == 1, "non-transient error must not be retried"


def test_retry_exhausts_then_reraises():
    """A persistently transient failure raises after exhausting attempts."""
    calls = {"n": 0}

    async def attempt():
        calls["n"] += 1
        raise Exception("overloaded_error")

    try:
        asyncio.run(_run_with_retry(attempt, max_attempts=3, backoff_s=0))
        assert False, "expected the transient error to propagate after exhaustion"
    except Exception as e:
        assert "overloaded" in str(e)
    assert calls["n"] == 3


# -- timeout ----------------------------------------------------------------

def test_drain_query_times_out(monkeypatch):
    """A query that never yields must raise TimeoutError, not hang forever."""

    def fake_query(prompt, options):
        async def gen():
            await asyncio.sleep(30)  # never completes within the test timeout
            yield "unreachable"
        return gen()

    monkeypatch.setattr(cac.cas, "query", fake_query)

    seen = []
    try:
        asyncio.run(
            _drain_query_with_timeout(
                prompt="hi", options=object(), handler=seen.append, timeout_s=0.05
            )
        )
        assert False, "expected TimeoutError"
    except asyncio.TimeoutError:
        pass
    assert seen == [], "nothing should have been consumed"


def test_drain_query_delivers_messages(monkeypatch):
    """Within the timeout, every message is handed to the handler in order."""

    def fake_query(prompt, options):
        async def gen():
            for m in ["a", "b", "c"]:
                yield m
        return gen()

    monkeypatch.setattr(cac.cas, "query", fake_query)

    seen = []
    asyncio.run(
        _drain_query_with_timeout(
            prompt="hi", options=object(), handler=seen.append, timeout_s=5
        )
    )
    assert seen == ["a", "b", "c"]
