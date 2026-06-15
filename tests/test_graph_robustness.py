"""Regression tests for the batch-1 graph-robustness fixes (audit findings #1-#4).

#1 + #2 -- supervisor research fan-out: one researcher failing must NOT cancel the
others or abort the whole phase (return_exceptions=True), and an unexpected error must
not be silently swallowed by the old `... or True` catch-all.
#3 -- recommended_recursion_limit scales the super-step budget to the iteration cap.
#4 -- the SDK concurrency semaphore is per-event-loop (no cross-loop reuse).

Dependency-free: supervisor_tools is exercised with hand-built messages and a fake
researcher subgraph, so no LLM/CLI/network calls occur.
"""
import asyncio

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END

import open_deep_research.claude_agent_chat as cac
import open_deep_research.deep_researcher as dr
from open_deep_research.deep_researcher import recommended_recursion_limit, supervisor_tools


def _cr(topic: str, call_id: str) -> dict:
    return {"name": "ConductResearch", "args": {"research_topic": topic},
            "id": call_id, "type": "tool_call"}


def _config() -> dict:
    return {"configurable": {"thread_id": "t", "max_concurrent_research_units": 5}}


# -- #1 + #2: per-unit isolation of researcher failures --------------------

def test_one_failing_researcher_does_not_lose_the_others(monkeypatch):
    """A raising research unit becomes a per-unit error; successful units survive."""
    async def fake_ainvoke(payload, config=None):
        topic = payload.get("research_topic", "")
        if "bad" in topic:
            raise RuntimeError("researcher boom")
        return {"compressed_research": f"FINDINGS for {topic}", "raw_notes": [f"raw {topic}"]}

    monkeypatch.setattr(dr.researcher_subgraph, "ainvoke", fake_ainvoke)

    messages = [
        SystemMessage(content="supervisor"),
        HumanMessage(content="brief"),
        AIMessage(content="", tool_calls=[_cr("good topic", "c1"), _cr("bad topic", "c2")]),
    ]
    state = {"supervisor_messages": messages, "research_iterations": 2, "research_brief": "b"}

    cmd = asyncio.run(supervisor_tools(state, _config()))

    # Did NOT abort to END (the old `gather` + `or True` would have, losing c1's work).
    assert cmd.goto == "supervisor"
    tool_msgs = {m.tool_call_id: m for m in cmd.update["supervisor_messages"]
                 if isinstance(m, ToolMessage)}
    assert "FINDINGS for good topic" in tool_msgs["c1"].content  # success preserved
    assert "failed" in tool_msgs["c2"].content.lower()           # failure isolated
    assert cmd.update.get("raw_notes") == ["raw good topic"]      # only successful raw notes


def _state_one_unit() -> dict:
    return {
        "supervisor_messages": [
            SystemMessage(content="supervisor"),
            HumanMessage(content="brief"),
            AIMessage(content="", tool_calls=[_cr("topic", "c1")]),
        ],
        "research_iterations": 2,
        "research_brief": "b",
    }


def test_unexpected_dispatch_error_is_raised_not_swallowed(monkeypatch):
    """An unexpected (non-token-limit) error in dispatch surfaces -- no `or True` masquerade.

    A researcher returning a non-dict makes ``observation.get(...)`` raise inside the
    aggregation loop, escaping into the except. With is_token_limit_exceeded False the
    handler must re-raise (the old code returned a fake clean END instead).
    """
    async def returns_bad(payload, config=None):
        return "not a dict"

    monkeypatch.setattr(dr.researcher_subgraph, "ainvoke", returns_bad)
    monkeypatch.setattr(dr, "is_token_limit_exceeded", lambda e, m: False)

    try:
        asyncio.run(supervisor_tools(_state_one_unit(), _config()))
        assert False, "unexpected dispatch error should propagate, not end as a clean run"
    except Exception:
        pass


def test_token_limit_ends_phase_gracefully(monkeypatch):
    """A genuine token-limit ends the research phase (goto END) rather than re-raising."""
    async def returns_bad(payload, config=None):
        return "not a dict"  # triggers the except path via AttributeError on .get()

    monkeypatch.setattr(dr.researcher_subgraph, "ainvoke", returns_bad)
    monkeypatch.setattr(dr, "is_token_limit_exceeded", lambda e, m: True)

    cmd = asyncio.run(supervisor_tools(_state_one_unit(), _config()))
    assert cmd.goto == END


# -- #3: recursion-limit recommendation ------------------------------------

def test_recommended_recursion_limit_scales_with_iterations():
    assert recommended_recursion_limit(6) == 49     # 4*6 + 25
    assert recommended_recursion_limit(10) == 65     # 4*10 + 25
    # Never below the floor: a zero/negative cap still yields a sane budget.
    assert recommended_recursion_limit(0) == 29      # 4*max(1,0) + 25
    assert recommended_recursion_limit(10) > 25      # exceeds LangGraph's default


# -- #4: per-event-loop semaphore ------------------------------------------

def test_semaphore_is_stable_within_a_loop():
    async def same() -> bool:
        return cac._semaphore() is cac._semaphore()
    assert asyncio.run(same()) is True


def test_semaphore_differs_across_loops():
    """Distinct event loops get distinct semaphores (no cross-loop reuse / bind error)."""
    async def get():
        return cac._semaphore()
    s1 = asyncio.run(get())   # one loop (created+closed by asyncio.run)
    s2 = asyncio.run(get())   # a different loop
    assert s1 is not s2, "a module-global singleton would reuse one bound to a dead loop"
