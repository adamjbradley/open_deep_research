import asyncio
from langchain_core.messages import AIMessage, HumanMessage
import open_deep_research.deep_researcher as dr


def _cfg():
    return {"configurable": {"max_researcher_iterations": 4, "allow_clarification": False}}


def test_blank_supervisor_turn_is_nudged_not_ended():
    # Supervisor's latest message has NO tool calls and no research has run yet.
    state = {
        "supervisor_messages": [
            HumanMessage(content="Research Brazil digital identity."),
            AIMessage(content="Here is some prose with no tool call."),
        ],
        "research_iterations": 1,
        "research_brief": "Research Brazil digital identity.",
    }
    cmd = asyncio.run(dr.supervisor_tools(state, _cfg()))
    assert cmd.goto == "supervisor"  # looped back, NOT __end__
    msgs = cmd.update["supervisor_messages"]
    assert msgs and "ConductResearch" in msgs[-1].content


# ---------------------------------------------------------------------------
# Task A2: _is_empty_run gate
# ---------------------------------------------------------------------------
from open_deep_research.deep_researcher import _is_empty_run


def test_is_empty_run_true_when_no_facts_and_no_sources():
    assert _is_empty_run(fact_count=0, raw_text_source_count=0) is True


def test_is_empty_run_false_when_any_facts():
    assert _is_empty_run(fact_count=3, raw_text_source_count=0) is False


def test_is_empty_run_false_when_sources_present():
    # sources gathered but 0 facts = "thin", NOT empty (don't auto-fail legitimately sparse countries)
    assert _is_empty_run(fact_count=0, raw_text_source_count=5) is False
