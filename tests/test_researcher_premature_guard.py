"""researcher_tools must not accept a premature ResearchComplete (no search yet).

Mirrors the supervisor_tools premature-completion guard: the CLI/subscription tool-selection
envelope can pick the no-argument ResearchComplete before any search runs, ending a researcher
unit with zero sources (the empty-dossier failure). The guard must withhold completion and loop
the researcher back to search, bounded by max_react_tool_calls.
"""
import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from open_deep_research import deep_researcher as dr


class _Tool:
    def __init__(self, name, search=False):
        self.name = name
        self.metadata = {"type": "search", "name": "web_search"} if search else {}


def _patch_tools(monkeypatch):
    tools = [_Tool("tavily_search", search=True), _Tool("think_tool"), _Tool("ResearchComplete")]

    async def fake_get_all_tools(config):
        return tools

    async def fake_exec(tool, args, config):
        return "ok"  # benign tool output (e.g. ResearchComplete/think executed)

    monkeypatch.setattr(dr, "get_all_tools", fake_get_all_tools)
    monkeypatch.setattr(dr, "execute_tool_safely", fake_exec)


_CFG = {"configurable": {"search_api": "tavily", "max_react_tool_calls": 4, "thread_id": "t"}}


def test_premature_research_complete_loops_back_to_search(monkeypatch):
    _patch_tools(monkeypatch)
    # researcher's first turn: ResearchComplete with NO prior search
    state = {
        "researcher_messages": [
            HumanMessage(content="Research Estonia's eID scheme."),
            AIMessage(content="", tool_calls=[{"name": "ResearchComplete", "args": {}, "id": "rc1"}]),
        ],
        "tool_call_iterations": 0,
    }
    cmd = asyncio.run(dr.researcher_tools(state, _CFG))
    assert cmd.goto == "researcher", "must loop back to search, not compress empty"
    msgs = cmd.update["researcher_messages"]
    nudge = [m for m in msgs if isinstance(m, ToolMessage) and m.tool_call_id == "rc1"]
    assert nudge and "search" in nudge[0].content.lower(), "must nudge the researcher to search"


def test_research_complete_allowed_after_a_search(monkeypatch):
    _patch_tools(monkeypatch)
    # a search already returned -> completion is legitimate
    state = {
        "researcher_messages": [
            HumanMessage(content="Research Estonia's eID scheme."),
            AIMessage(content="", tool_calls=[{"name": "tavily_search", "args": {"queries": ["x"]}, "id": "s1"}]),
            ToolMessage(content="results...", name="tavily_search", tool_call_id="s1"),
            AIMessage(content="", tool_calls=[{"name": "ResearchComplete", "args": {}, "id": "rc2"}]),
        ],
        "tool_call_iterations": 1,
    }
    cmd = asyncio.run(dr.researcher_tools(state, _CFG))
    assert cmd.goto == "compress_research", "completion after a real search is allowed"
