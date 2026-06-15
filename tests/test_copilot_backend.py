"""Coverage for the GitHub Copilot CLI backend (model role + web search + routing).

Targets the standalone agentic ``copilot`` CLI (Copilot subscription), wired to mirror
the existing Gemini/Codex CLI backends. Dependency-free: the CLI subprocess (_run_cli)
is mocked, so no real ``copilot`` binary, login, or network is needed -- the tests
verify the wiring (provider routing, model construction, plain + tool-envelope
generation, graceful search-failure), not the real CLI's flags.
"""
import asyncio

from langchain_core.messages import HumanMessage

import open_deep_research.claude_agent_chat as cac
from open_deep_research.claude_agent_chat import (
    CopilotCLIChat,
    build_chat_model,
    parse_backend,
    to_copilot_model,
)


# -- routing / construction ------------------------------------------------

def test_parse_backend_routes_copilot_prefixes():
    assert parse_backend("copilot:gpt-4.1") == ("copilot", "gpt-4.1")
    assert parse_backend("github:claude-sonnet-4.5") == ("copilot", "claude-sonnet-4.5")
    assert parse_backend("copilot") == ("copilot", "copilot")


def test_build_chat_model_returns_copilot_backend():
    m = build_chat_model("copilot:gpt-4.1")
    assert isinstance(m, CopilotCLIChat)
    assert m.model == "gpt-4.1"


def test_to_copilot_model_strips_prefix_and_passes_through():
    assert to_copilot_model("copilot:gpt-4.1") == "gpt-4.1"
    assert to_copilot_model("github:claude-sonnet-4.5") == "claude-sonnet-4.5"
    assert to_copilot_model("copilot") == ""   # bare -> CLI default
    assert to_copilot_model("gpt-4.1") == "gpt-4.1"


# -- model generation (plain + tool envelope) ------------------------------

def test_copilot_plain_generation(monkeypatch):
    async def fake_run_cli(cmd, env=None, stdin=None, timeout=600, cwd=None):
        assert "copilot" in cmd[0]
        return "Paris is the capital of France."

    monkeypatch.setattr(cac, "_run_cli", fake_run_cli)
    model = CopilotCLIChat(model="gpt-4.1")
    msg = asyncio.run(model.ainvoke([HumanMessage(content="capital of France?")]))
    assert "Paris" in msg.content


def test_copilot_tool_envelope_is_parsed(monkeypatch):
    # The CLI has no native schema enforcement, so the backend coerces the JSON
    # tool-selection envelope from the prompt and parses it back into tool_calls.
    envelope = '{"tool_calls": [{"name": "ConductResearch", "arguments": {"research_topic": "India digital id"}}]}'

    async def fake_run_cli(cmd, env=None, stdin=None, timeout=600, cwd=None):
        return envelope

    monkeypatch.setattr(cac, "_run_cli", fake_run_cli)

    from langchain_core.tools import tool

    @tool
    def ConductResearch(research_topic: str) -> str:
        """Delegate a research topic."""
        return ""

    model = CopilotCLIChat(model="gpt-4.1").bind_tools([ConductResearch])
    msg = asyncio.run(model.ainvoke([HumanMessage(content="research india")]))
    assert msg.tool_calls and msg.tool_calls[0]["name"] == "ConductResearch"
    assert msg.tool_calls[0]["args"]["research_topic"] == "India digital id"


# -- web search ------------------------------------------------------------

def test_copilot_search_returns_cli_output(monkeypatch):
    async def fake_run_cli(cmd, env=None, stdin=None, timeout=600, cwd=None):
        return "Copilot findings: result [example.com]"

    monkeypatch.setattr(cac, "_run_cli", fake_run_cli)
    out = asyncio.run(cac.run_copilot_search(["q"]))
    assert "Copilot findings" in out


def test_copilot_search_handles_failure_gracefully(monkeypatch):
    async def boom(cmd, env=None, stdin=None, timeout=600, cwd=None):
        raise RuntimeError("copilot CLI not installed")  # non-transient -> not retried

    monkeypatch.setattr(cac, "_run_cli", boom)
    out = asyncio.run(cac.run_copilot_search(["q"]))
    assert "Error performing Copilot web search" in out
