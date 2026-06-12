"""Regression tests for the supervisor's premature-ResearchComplete guard.

Background: the CLI/subscription backends select tools via a JSON envelope that
constrains only the tool name, not its arguments. The no-argument
``ResearchComplete`` is therefore the shortest valid selection, and the
supervisor would emit it on turn 1 -- ending the research phase before any
``ConductResearch`` ran, leaving ``notes``/``raw_notes`` empty. ``supervisor_tools``
now blocks that and loops back. See ``supervisor_tools`` in ``deep_researcher.py``.

These tests are fast and dependency-free: ``supervisor_tools`` is exercised
directly with hand-built messages, so no LLM, CLI, or network calls occur. We
only cover the branches that do NOT dispatch ``ConductResearch`` (which would
spawn a real researcher subgraph); the guard and the completion paths are pure.
"""
import asyncio

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END

from open_deep_research.deep_researcher import supervisor_tools


def _config() -> dict:
    """Minimal config; Configuration fills the rest from defaults (max iterations = 6)."""
    return {"configurable": {"thread_id": "test"}}


def _research_complete_call(call_id: str = "call_rc") -> dict:
    return {"name": "ResearchComplete", "args": {}, "id": call_id, "type": "tool_call"}


def _conduct_research_call(topic: str, call_id: str = "call_cr") -> dict:
    return {
        "name": "ConductResearch",
        "args": {"research_topic": topic},
        "id": call_id,
        "type": "tool_call",
    }


def test_premature_research_complete_is_blocked():
    """ResearchComplete on the first turn, with no prior research, must NOT end the phase.

    Without the guard this hits the exit block and routes to END; with it, the
    supervisor is nudged back to dispatch real research.
    """
    messages = [
        SystemMessage(content="supervisor prompt"),
        HumanMessage(content="research brief"),
        AIMessage(content="", tool_calls=[_research_complete_call()]),
    ]
    state = {
        "supervisor_messages": messages,
        "research_iterations": 1,
        "research_brief": "brief",
    }

    cmd = asyncio.run(supervisor_tools(state, _config()))

    assert cmd.goto == "supervisor", "premature completion should loop back, not END"
    corrective = cmd.update["supervisor_messages"]
    assert len(corrective) == 1
    nudge = corrective[0]
    assert isinstance(nudge, ToolMessage)
    assert nudge.name == "ResearchComplete"
    assert nudge.tool_call_id == "call_rc"  # answers the actual tool call
    assert "ConductResearch" in nudge.content  # tells the model what to do instead


def test_research_complete_allowed_after_research():
    """Once a ConductResearch result exists, ResearchComplete legitimately ends the phase."""
    messages = [
        SystemMessage(content="supervisor prompt"),
        HumanMessage(content="research brief"),
        AIMessage(content="", tool_calls=[_conduct_research_call("history of X")]),
        ToolMessage(content="found: A, B, C", name="ConductResearch", tool_call_id="call_cr"),
        AIMessage(content="", tool_calls=[_research_complete_call()]),
    ]
    state = {
        "supervisor_messages": messages,
        "research_iterations": 2,
        "research_brief": "brief",
    }

    cmd = asyncio.run(supervisor_tools(state, _config()))

    assert cmd.goto == END, "completion after real research should end the phase"
    assert "found: A, B, C" in cmd.update["notes"]  # research carried through to notes


def test_no_tool_calls_still_ends():
    """An empty tool-call envelope still terminates; the guard only intercepts ResearchComplete."""
    messages = [
        SystemMessage(content="supervisor prompt"),
        HumanMessage(content="research brief"),
        AIMessage(content="nothing to do", tool_calls=[]),
    ]
    state = {
        "supervisor_messages": messages,
        "research_iterations": 1,
        "research_brief": "brief",
    }

    cmd = asyncio.run(supervisor_tools(state, _config()))

    assert cmd.goto == END
