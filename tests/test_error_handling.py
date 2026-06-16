"""Regression tests for the error-handling hardening (timeouts + tool/LLM/search errors).

Dependency-free: backends/tools are faked, DB hits a temp file. Covers the
behavior-changing fixes; pure logging additions are exercised indirectly.
"""
import asyncio

import aiosqlite
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import open_deep_research.claude_agent_chat as cac
import open_deep_research.deep_researcher as dr
from open_deep_research import storage, utils
from open_deep_research.deep_researcher import (
    COMPRESSION_FAILED_SENTINEL,
    _report_is_failed,
)
from open_deep_research.factbase import migrations, schema


# -- X1 detector -----------------------------------------------------------

def test_report_is_failed_detects_sentinels_and_empty():
    assert _report_is_failed("") is True
    assert _report_is_failed("   ") is True
    assert _report_is_failed(None) is True
    assert _report_is_failed(COMPRESSION_FAILED_SENTINEL) is True
    assert _report_is_failed("Error generating final report: boom") is True
    assert _report_is_failed("# India Digital Identity\n\nAadhaar covers ~99%...") is False


# -- B4: token-limit detection on the CLI/subscription backends ------------

def test_is_token_limit_exceeded_matches_generic_overflow_text():
    # Plain RuntimeError (as the CLI/SDK backends raise) -- no provider class/prefix.
    assert utils.is_token_limit_exceeded(RuntimeError("prompt is too long: 250000 tokens")) is True
    assert utils.is_token_limit_exceeded(RuntimeError("maximum context length exceeded")) is True
    assert utils.is_token_limit_exceeded(RuntimeError("connection reset by peer")) is False


# -- B1: CLI backend must not silently return empty tool_calls -------------

def test_cli_backend_raises_when_no_envelope(monkeypatch):
    """If a CLI model never emits a parseable tool envelope, raise (don't return []) -
    empty tool_calls would be read by the graph as 'done' and silently end the phase."""
    monkeypatch.setenv("CLI_TOOL_RETRIES", "2")

    class _FakeCLI(cac._CLIJsonChat):
        _backend_name = "fake-cli"

        async def _backend_generate(self, system_prompt, prompt, schema):
            return "Sorry, I can't comply.", None  # prose, no JSON envelope

    from langchain_core.tools import tool

    @tool
    def DoThing(x: str) -> str:
        """A tool."""
        return ""

    model = _FakeCLI(model="m").bind_tools([DoThing])
    try:
        asyncio.run(model.ainvoke([HumanMessage(content="go")]))
        assert False, "expected ValueError when no envelope parses"
    except ValueError as e:
        assert "tool-call envelope" in str(e)


# -- A2: unknown tool name is handled, not a KeyError crash ----------------

def test_researcher_tools_handles_unknown_tool_name(monkeypatch):
    async def fake_tools(config):
        return [utils.think_tool]  # only think_tool is available

    monkeypatch.setattr(dr, "get_all_tools", fake_tools)

    bad_call = {"name": "NonexistentSearch", "args": {"q": "x"}, "id": "c1", "type": "tool_call"}
    state = {
        "researcher_messages": [
            SystemMessage(content="sys"),
            AIMessage(content="", tool_calls=[bad_call]),
        ],
        "tool_call_iterations": 0,
        "research_topic": "t",
    }
    cmd = asyncio.run(dr.researcher_tools(state, {"configurable": {"thread_id": "t"}}))

    # Did not crash; produced a corrective ToolMessage naming the bad tool.
    msgs = cmd.update["researcher_messages"]
    nudge = [m for m in msgs if getattr(m, "tool_call_id", None) == "c1"][0]
    assert "not an available tool" in nudge.content


# -- X1: a failed report is logged as error, NOT merged into the dossier ---

def test_persist_skips_dossier_for_failed_report(tmp_path):
    db = str(tmp_path / "f.db")

    async def run():
        async with aiosqlite.connect(db) as conn:
            await storage._ensure_schema(conn)
            await migrations.apply(conn, schema.STEPS)

        state = {
            "messages": [HumanMessage(content="What is the digital id scheme of Atlantis?")],
            "research_brief": "brief",
            "final_report": COMPRESSION_FAILED_SENTINEL,  # a failure sentinel
            "raw_notes": [],
        }
        cfg = {"configurable": {"thread_id": "t-fail", "database_path": db,
                                "persist_results": True}}
        out = await dr.persist_research(state, cfg)
        assert "report_id" in out

        async with aiosqlite.connect(db) as conn:
            conn.row_factory = aiosqlite.Row
            runs = [dict(r) for r in await (await conn.execute(
                "SELECT status, error FROM research_runs")).fetchall()]
            subjects = list(await (await conn.execute("SELECT COUNT(*) FROM subjects")).fetchone())

        assert any(r["status"] == "error" for r in runs), "failed run must be status=error"
        assert subjects[0] == 0, "no subject dossier should be created/merged for a failed report"

    asyncio.run(run())
