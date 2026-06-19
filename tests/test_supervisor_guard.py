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

from open_deep_research.deep_researcher import (
    ConductResearch,
    ResearchComplete,
    _lead_researcher_tools,
    supervisor_tools,
    think_tool,
)


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


def test_research_complete_withheld_until_research_conducted():
    """Pre-research, the supervisor is offered ONLY ConductResearch.

    The CLI/subscription envelope constrains tool *name* but not arguments, so the
    no-arg ResearchComplete is always a valid selection -- the model picks it every
    turn and the premature-completion guard just loops until the iteration cap, leaving
    notes empty. Withholding ResearchComplete until a ConductResearch result exists
    forces a real dispatch first.
    """
    pre = _lead_researcher_tools(conducted_research=False)
    assert ConductResearch in pre
    assert ResearchComplete not in pre, "ResearchComplete must be withheld pre-research"

    post = _lead_researcher_tools(conducted_research=True)
    assert ConductResearch in post
    assert ResearchComplete in post, "ResearchComplete becomes available after research"
    assert think_tool in post


def test_empty_research_topic_is_rejected():
    """A ConductResearch call with an empty research_topic must not dispatch.

    The envelope can produce an argument-less ConductResearch (the forced selection
    pre-research). Dispatching it would KeyError on args["research_topic"]; instead the
    call is answered with a corrective nudge and the supervisor loops back -- no
    researcher subgraph runs, so this test stays pure.
    """
    empty_call = {
        "name": "ConductResearch",
        "args": {},
        "id": "call_empty",
        "type": "tool_call",
    }
    messages = [
        SystemMessage(content="supervisor prompt"),
        HumanMessage(content="research brief"),
        AIMessage(content="", tool_calls=[empty_call]),
    ]
    state = {
        "supervisor_messages": messages,
        "research_iterations": 1,
        "research_brief": "brief",
    }

    cmd = asyncio.run(supervisor_tools(state, _config()))

    assert cmd.goto == "supervisor", "empty-topic research must loop back, not dispatch/END"
    nudges = cmd.update["supervisor_messages"]
    assert len(nudges) == 1
    nudge = nudges[0]
    assert isinstance(nudge, ToolMessage)
    assert nudge.name == "ConductResearch"
    assert nudge.tool_call_id == "call_empty"
    assert "research_topic" in nudge.content
    assert "raw_notes" not in cmd.update, "nothing should have been researched"


def test_no_tool_calls_before_research_is_nudged():
    """A blank turn (no tool calls) before any ConductResearch loops back with a corrective nudge.

    Previously this hit the no_tool_calls exit block and routed to END, producing an empty
    dossier (the 'Brazil' failure). The blank-turn guard now intercepts it and loops to supervisor.
    """
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

    assert cmd.goto == "supervisor", "blank turn before any research must nudge back, not END"
    msgs = cmd.update["supervisor_messages"]
    assert msgs and "ConductResearch" in msgs[-1].content


def test_no_tool_calls_after_research_still_ends():
    """A blank turn AFTER research has already run terminates normally."""
    messages = [
        SystemMessage(content="supervisor prompt"),
        HumanMessage(content="research brief"),
        AIMessage(content="delegating", tool_calls=[{"name": "ConductResearch", "id": "call_cr", "args": {"research_topic": "test"}}]),
        ToolMessage(content="found: A, B, C", name="ConductResearch", tool_call_id="call_cr"),
        AIMessage(content="nothing more to do", tool_calls=[]),
    ]
    state = {
        "supervisor_messages": messages,
        "research_iterations": 2,
        "research_brief": "brief",
    }

    cmd = asyncio.run(supervisor_tools(state, _config()))

    assert cmd.goto == END, "blank turn after real research should end the phase"
