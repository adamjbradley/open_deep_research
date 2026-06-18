"""The CLI drain must not freeze the run when a subprocess wedges in uninterruptible I/O.

A plain asyncio.wait_for fires its timeout but then awaits the inner task's cancellation,
which blocks tearing down a wedged subprocess -> the whole run hangs. The hardened
_drain_query_with_timeout bounds that teardown and ABANDONS a wedged task instead.
"""
import asyncio

import pytest

from open_deep_research import claude_agent_chat as cac


class _SlowTeardownQuery:
    """Never yields a message; when cancelled, its teardown ignores the cancel for ~5s
    -- standing in for a CLI subprocess that won't die promptly on timeout."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            await asyncio.sleep(3600)  # never produces a message
        except asyncio.CancelledError:
            try:
                await asyncio.sleep(5)  # teardown that doesn't respond to the cancel promptly
            except asyncio.CancelledError:
                pass
            raise


def test_drain_abandons_wedged_subprocess_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(cac.cas, "query", lambda prompt, options: _SlowTeardownQuery())
    monkeypatch.setattr(cac, "_DRAIN_REAP_GRACE_S", 0.2)

    async def _run():
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        with pytest.raises(asyncio.TimeoutError):
            await cac._drain_query_with_timeout("p", None, lambda m: None, timeout_s=0.3)
        return loop.time() - t0

    elapsed = asyncio.run(_run())
    # Must return ~ timeout(0.3)+grace(0.2); the ~5s wedged teardown must NOT be awaited.
    assert elapsed < 2.0, f"drain hung {elapsed:.1f}s instead of abandoning the wedged subprocess"


def test_drain_returns_normally_on_success(monkeypatch):
    class _OkQuery:
        def __init__(self):
            self._sent = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._sent:
                raise StopAsyncIteration
            self._sent = True
            return "msg-1"

    got = []
    monkeypatch.setattr(cac.cas, "query", lambda prompt, options: _OkQuery())
    asyncio.run(cac._drain_query_with_timeout("p", None, got.append, timeout_s=5))
    assert got == ["msg-1"]


def test_drain_propagates_real_error(monkeypatch):
    class _BoomQuery:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RuntimeError("backend exploded")

    monkeypatch.setattr(cac.cas, "query", lambda prompt, options: _BoomQuery())
    with pytest.raises(RuntimeError, match="backend exploded"):
        asyncio.run(cac._drain_query_with_timeout("p", None, lambda m: None, timeout_s=5))
