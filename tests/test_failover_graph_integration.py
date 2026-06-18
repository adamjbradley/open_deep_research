"""Compiled-graph integration test for the thread_id-registry failover fix.

LangGraph runs each node in its own copied context, so a ContextVar set in the
entry node is invisible to later nodes.  The fix stores the tracker in a
module-level dict keyed by thread_id so it survives across node boundaries.

This test would have caught the original bug.
"""
import asyncio

import pytest
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from open_deep_research import claude_agent_chat as cac
from open_deep_research.claude_agent_chat import configurable_claude_model
from open_deep_research.failover import get_tracker, new_run_tracker


@pytest.fixture(autouse=True)
def _disable_health_file(monkeypatch):
    """Disable the health file for all integration tests to avoid cross-test pollution."""
    monkeypatch.setenv("ODR_BACKEND_HEALTH", "off")


class _FakeModel:
    def __init__(self, model_id, script):
        self.model_id = model_id
        self.script = script

    def with_structured_output(self, *a, **k):
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


class _S(TypedDict, total=False):
    thread_id: str
    result: str
    sticky_seen: bool
    persisted_failovers: int


def test_tracker_survives_across_graph_nodes(monkeypatch):
    """The registry (keyed by thread_id) must make a stage's down-mark + recorded
    failover visible in a LATER node — the cross-node guarantee a ContextVar can't give."""
    script = {
        "gemini:gemini-2.5-pro": Exception("429 quota exceeded"),
        "claude-opus-4-8": "BACKUP-OK",
    }
    constructed = []

    def fake_build(model_string, max_tokens=None):
        constructed.append(model_string)
        return _FakeModel(model_string, script)
    monkeypatch.setattr(cac, "build_chat_model", fake_build)

    def entry(state, config):
        tid = (config.get("configurable") or {}).get("thread_id")
        new_run_tracker(tid)
        return {"thread_id": tid}

    async def stage(state, config):
        model = configurable_claude_model().with_config({
            "model_chain": ["gemini:gemini-2.5-pro", "claude-opus-4-8"],
            "stage": "supervisor",
        })
        out = await model.ainvoke("hi")
        return {"result": out}

    def finish(state, config):
        tid = state["thread_id"]
        t = get_tracker(tid)
        # the dead primary marked down in `stage` must still be down here (cross-node)
        return {"sticky_seen": t.is_down("gemini:gemini-2.5-pro"),
                "persisted_failovers": len(t.failovers)}

    g = StateGraph(_S)
    g.add_node("entry", entry)
    g.add_node("stage", stage)
    g.add_node("finish", finish)
    g.add_edge(START, "entry")
    g.add_edge("entry", "stage")
    g.add_edge("stage", "finish")
    g.add_edge("finish", END)
    app = g.compile()

    out = asyncio.run(app.ainvoke({}, config={"configurable": {"thread_id": "run-123"}}))
    assert out["result"] == "BACKUP-OK"          # failover happened
    assert out["sticky_seen"] is True            # down-mark survived into a later node
    assert out["persisted_failovers"] == 1       # the failover record is visible cross-node


def test_all_research_failed_sentinel_is_a_failed_report():
    from open_deep_research.deep_researcher import (
        ALL_RESEARCH_FAILED_SENTINEL, _report_is_failed,
    )
    assert _report_is_failed(ALL_RESEARCH_FAILED_SENTINEL) is True


def test_partial_success_is_not_failed():
    from open_deep_research.deep_researcher import _report_is_failed
    assert _report_is_failed("Real findings about India digital ID...") is False
