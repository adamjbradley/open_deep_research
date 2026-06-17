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
