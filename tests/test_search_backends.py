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
from unittest.mock import AsyncMock, patch

import claude_agent_sdk as cas

import open_deep_research.claude_agent_chat as cac
from open_deep_research import utils


# -- _acquire_tavily + _finalize_search (Task-1 refactor) ------------------

def _tav_resp(query, urls):
    return {"query": query, "results": [
        {"url": u, "title": f"T-{u}", "content": f"snippet-{u}", "raw_content": f"RAW-{u}"} for u in urls]}


def test_acquire_tavily_normalizes_and_dedups():
    with patch.object(utils, "tavily_search_async", new=AsyncMock(
            return_value=[_tav_resp("q", ["http://a", "http://b", "http://a"])])):
        out = asyncio.run(utils._acquire_tavily(["q"], 5, "general", None))
    assert set(out) == {"http://a", "http://b"}                 # dedup by URL
    rec = out["http://a"]
    assert rec["url"] == "http://a" and rec["title"] == "T-http://a"
    assert rec["raw_content"] == "RAW-http://a" and rec["query"] == "q"   # normalized contract


def test_finalize_formats_and_records(monkeypatch):
    uniq = {"http://a": {"url": "http://a", "title": "TA", "content": "CA", "raw_content": "RA", "query": "q"}}
    monkeypatch.setattr(utils, "record_search_sources", AsyncMock())
    # summarize OFF -> no model call; uses 'content'
    monkeypatch.setattr(utils.Configuration, "from_runnable_config",
                        lambda c: type("C", (), {"summarize_search_results": False, "max_content_length": 5000,
                        "max_search_results": 5, "summarization_model": "claude:haiku",
                        "summarization_model_max_tokens": 1000, "model_chain": lambda *a, **k: ["claude:haiku"],
                        "persist_results": False, "max_structured_output_retries": 3})())
    out = asyncio.run(utils._finalize_search(uniq, None))
    assert "http://a" in out and "TA" in out and "CA" in out


def test_finalize_summarizes_raw_content_and_records(monkeypatch):
    uniq = {"http://e": {"url": "http://e", "title": "TE", "content": "snippet",
                         "raw_content": "FULL-EXA-TEXT", "query": "q"}}
    recorded = {}
    async def _fake_record(store, tid, results):
        recorded["results"] = results
    monkeypatch.setattr(utils, "record_search_sources", _fake_record)
    seen = {}
    async def _fake_summarize(model, text):
        seen["text"] = text
        return "SUMMARized"
    monkeypatch.setattr(utils, "summarize_webpage", _fake_summarize)
    monkeypatch.setattr(utils.Configuration, "from_runnable_config",
        lambda c: type("C", (), {"summarize_search_results": True, "max_content_length": 5000,
        "max_search_results": 5, "summarization_model": "claude:haiku",
        "summarization_model_max_tokens": 1000, "max_structured_output_retries": 3,
        "model_chain": lambda *a, **k: ["claude:haiku"], "persist_results": False})())
    out = asyncio.run(utils._finalize_search(uniq, None))
    assert seen["text"] == "FULL-EXA-TEXT"      # raw text (not the snippet) reaches the summarizer
    assert "SUMMARized" in out


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


# -- Exa search (_acquire_exa) ------------------------------------------

class _FakeExaResult:
    def __init__(self, url, title, text, summary):
        self.url, self.title, self.text, self.summary = url, title, text, summary


class _FakeExaResp:
    def __init__(self, results):
        self.results = results


class _FakeExa:
    def __init__(self, *a, **k):
        pass

    def search_and_contents(self, q, **k):
        return _FakeExaResp([_FakeExaResult("http://x", "TX", "FULLTEXT", "SUMMARY")])


def test_acquire_exa_normalizes(monkeypatch):
    monkeypatch.setattr(utils, "Exa", _FakeExa, raising=False)
    monkeypatch.setenv("EXA_API_KEY", "k")
    out = asyncio.run(utils._acquire_exa(["q"], 5, "general", None))
    rec = out["http://x"]
    assert rec["raw_content"] == "FULLTEXT"     # text -> raw_content
    assert rec["content"] == "SUMMARY"          # summary -> content
    assert rec["title"] == "TX" and rec["query"] == "q"


def test_acquire_exa_errors_return_empty(monkeypatch):
    class _Boom:
        def __init__(self, *a, **k):
            pass

        def search_and_contents(self, *a, **k):
            raise RuntimeError("api down")

    monkeypatch.setattr(utils, "Exa", _Boom, raising=False)
    monkeypatch.setenv("EXA_API_KEY", "k")
    assert asyncio.run(utils._acquire_exa(["q"], 5, "general", None)) == {}


def test_get_exa_api_key_respects_config_gate(monkeypatch):
    # default (env-only): returns env, ignores config apiKeys
    monkeypatch.delenv("GET_API_KEYS_FROM_CONFIG", raising=False)
    monkeypatch.setenv("EXA_API_KEY", "env-key")
    cfg = {"configurable": {"apiKeys": {"EXA_API_KEY": "cfg-key"}}}
    assert utils.get_exa_api_key(cfg) == "env-key"
    # gate on -> config wins
    monkeypatch.setenv("GET_API_KEYS_FROM_CONFIG", "true")
    assert utils.get_exa_api_key(cfg) == "cfg-key"


# -- Hybrid search (_acquire_hybrid) ----------------------------------------

def _mk(url): return {"url": url, "title": f"T{url}", "content": f"c{url}", "raw_content": f"r{url}", "query": "q"}

def test_hybrid_interleaves_exa_first_dedups_caps(monkeypatch):
    tav = {"http://a": _mk("http://a"), "http://b": _mk("http://b")}
    exa = {"http://x": _mk("http://x"), "http://a": _mk("http://a")}  # 'a' overlaps tavily
    monkeypatch.setattr(utils, "_acquire_tavily", AsyncMock(return_value=tav))
    monkeypatch.setattr(utils, "_acquire_exa", AsyncMock(return_value=exa))
    out = asyncio.run(utils._acquire_hybrid(["q"], 3, "general", None))
    assert list(out) == ["http://x", "http://a", "http://b"]   # exa-first, dedup 'a', cap 3
    out2 = asyncio.run(utils._acquire_hybrid(["q"], 2, "general", None))
    assert len(out2) == 2 and list(out2)[0] == "http://x"       # cap respected

def test_hybrid_degrades_when_exa_empty(monkeypatch):
    monkeypatch.setattr(utils, "_acquire_tavily", AsyncMock(return_value={"http://a": _mk("http://a")}))
    monkeypatch.setattr(utils, "_acquire_exa", AsyncMock(return_value={}))
    out = asyncio.run(utils._acquire_hybrid(["q"], 5, "general", None))
    assert list(out) == ["http://a"]                            # tavily-only


# -- Get search tool dispatch (get_search_tool: EXA, TAVILY_EXA) ---------

def test_get_search_tool_returns_exa_and_hybrid():
    from open_deep_research.configuration import SearchAPI
    assert asyncio.run(utils.get_search_tool(SearchAPI.EXA)) == [utils.exa_search]
    assert asyncio.run(utils.get_search_tool(SearchAPI.TAVILY_EXA)) == [utils.tavily_exa_search]
