"""Coverage for the LLM-driven search backends: Claude SDK, Gemini CLI, Codex CLI, Tavily.

Dependency-free: the SDK query, the CLI subprocess (_run_cli), and the Tavily API
(tavily_search_async) are all mocked, so no real network / login / subprocess runs.
Each test asserts the backend (a) assembles results on success and (b) degrades
gracefully on failure -- returning an error string rather than crashing the research
loop, the property the researcher relies on.

(Fact extraction itself is covered by tests/test_factbase_extractor.py; its graph wiring
by tests/test_extract_facts_backfill_wiring.py and tests/test_graph_extract_facts_wiring.py.)
"""
import asyncio

import claude_agent_sdk as cas

import open_deep_research.claude_agent_chat as cac
from open_deep_research import utils


# -- Claude SDK web search (run_search_agent) ------------------------------

def _assistant(text: str):
    return cas.AssistantMessage(content=[cas.TextBlock(text=text)], model="haiku")


def _result(text: str = "", is_error: bool = False):
    return cas.ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=is_error,
        num_turns=1, session_id="s", result=(text or None),
    )


def test_claude_search_returns_compiled_findings(monkeypatch):
    def fake_query(prompt, options):
        async def gen():
            yield _assistant("Finding: India Aadhaar coverage ~99% [uidai.gov.in]")
            yield _result()
        return gen()

    monkeypatch.setattr(cac.cas, "query", fake_query)
    out = asyncio.run(cac.run_search_agent(["india digital id coverage"]))
    assert "Aadhaar coverage ~99%" in out


def test_claude_search_reports_error_result_gracefully(monkeypatch):
    def fake_query(prompt, options):
        async def gen():
            yield _result(text="upstream failure", is_error=True)
        return gen()

    monkeypatch.setattr(cac.cas, "query", fake_query)
    out = asyncio.run(cac.run_search_agent(["q"]))
    assert "Error performing Claude Code web search" in out


# -- Gemini CLI web search (run_gemini_search) -----------------------------

def test_gemini_search_returns_cli_output(monkeypatch):
    async def fake_run_cli(cmd, env=None, stdin=None, timeout=600, cwd=None):
        return "Gemini findings: grounded result [example.com]"

    monkeypatch.setattr(cac, "_run_cli", fake_run_cli)
    out = asyncio.run(cac.run_gemini_search(["q"]))
    assert "Gemini findings" in out


def test_gemini_search_handles_failure_gracefully(monkeypatch):
    async def boom(cmd, env=None, stdin=None, timeout=600, cwd=None):
        raise RuntimeError("gemini CLI not found")

    monkeypatch.setattr(cac, "_run_cli", boom)
    out = asyncio.run(cac.run_gemini_search(["q"]))
    assert "Error performing Gemini web search" in out


# -- Codex CLI web search (run_codex_search) -------------------------------

def test_codex_search_reads_last_message_file(monkeypatch):
    async def fake_run_cli(cmd, env=None, stdin=None, timeout=600, cwd=None):
        # Codex writes its final message to the --output-last-message path; emulate that.
        out_path = cmd[cmd.index("--output-last-message") + 1]
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("Codex findings: web-search result [example.org]")

    monkeypatch.setattr(cac, "_run_cli", fake_run_cli)
    out = asyncio.run(cac.run_codex_search(["q"]))
    assert "Codex findings" in out


def test_codex_search_handles_failure_gracefully(monkeypatch):
    async def boom(cmd, env=None, stdin=None, timeout=600, cwd=None):
        raise RuntimeError("codex CLI unavailable")  # non-transient -> not retried

    monkeypatch.setattr(cac, "_run_cli", boom)
    out = asyncio.run(cac.run_codex_search(["q"]))
    assert "Error performing Codex web search" in out


# -- Tavily search (tavily_search) -----------------------------------------

def test_tavily_search_dedupes_and_formats(monkeypatch):
    """Results are deduped by URL and rendered; no model call when raw_content is absent."""
    async def fake_async(queries, **kw):
        return [
            {"query": "q1", "results": [
                {"url": "https://a.org", "title": "A", "content": "summary A"},
                {"url": "https://dup.org", "title": "Dup", "content": "summary D"},
            ]},
            {"query": "q2", "results": [
                {"url": "https://dup.org", "title": "Dup-again", "content": "summary D2"},
                {"url": "https://b.org", "title": "B", "content": "summary B"},
            ]},
        ]

    monkeypatch.setattr(utils, "tavily_search_async", fake_async)
    # config=None -> run_source DB capture is skipped; results lack raw_content so the
    # summarization model is never invoked (noop path). .coroutine unwraps the @tool.
    out = asyncio.run(utils.tavily_search.coroutine(queries=["q1", "q2"], config=None))

    assert "https://a.org" in out and "https://b.org" in out
    assert out.count("https://dup.org") == 1, "duplicate URL must appear once"
    assert "summary A" in out


def test_tavily_search_reports_no_results(monkeypatch):
    async def fake_async(queries, **kw):
        return [{"query": "q1", "results": []}]

    monkeypatch.setattr(utils, "tavily_search_async", fake_async)
    out = asyncio.run(utils.tavily_search.coroutine(queries=["q1"], config=None))
    assert "No valid search results found" in out
