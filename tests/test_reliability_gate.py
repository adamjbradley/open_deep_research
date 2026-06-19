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
