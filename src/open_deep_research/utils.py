"""Utility functions and helpers for the Deep Research agent."""

import asyncio
import logging
import os
import warnings
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Dict, List, Literal, Optional
from weakref import WeakKeyDictionary

import aiohttp
from dotenv import load_dotenv
from langchain_core.language_models import BaseChatModel

# Load environment variables from .env file
load_dotenv()
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    MessageLikeRepresentation,
    filter_messages,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import (
    BaseTool,
    InjectedToolArg,
    StructuredTool,
    ToolException,
    tool,
)
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.config import get_store
from mcp import McpError
from tavily import AsyncTavilyClient
from exa_py import Exa

from open_deep_research.claude_agent_chat import (
    ClaudeAgentChat,
    configurable_claude_model,
    run_codex_search,
    run_gemini_search,
    run_search_agent,
    to_claude_model,
    to_codex_model,
    to_gemini_model,
    use_subscription,
)
from open_deep_research.configuration import Configuration, SearchAPI
from open_deep_research.prompts import summarize_webpage_prompt
from open_deep_research.state import ResearchComplete, Summary

logger = logging.getLogger(__name__)


async def record_search_sources(run_source_store, thread_id: str, unique_results: dict) -> None:
    """Persist each unique search result as a run_source row (raw_text if raw_content present)."""
    for url, result in unique_results.items():
        raw = (result or {}).get("raw_content") or ""
        if raw:
            await run_source_store.record(thread_id, url, raw, capture_status="raw_text")
        else:
            await run_source_store.record(thread_id, url, None, capture_status="summarized")


##########################
# Tavily Search Tool Utils
##########################
TAVILY_SEARCH_DESCRIPTION = (
    "A search engine optimized for comprehensive, accurate, and trusted results. "
    "Useful for when you need to answer questions about current events."
)
# Bound how many summarize_webpage calls are IN FLIGHT at once, process-wide per event
# loop. Each summarize_webpage has its own wall-clock timeout; if we fire one coroutine
# per search result (often 20-40) they all start their timers together, then queue on the
# model backend's own concurrency limit (e.g. the ~7 Claude SDK slots, or whatever Gemini
# allows) and the excess age out before they ever execute. Gating ENTRY here -- the
# `async with` sits OUTSIDE summarize_webpage's timeout -- means a call's timer only starts
# once a slot is free, so each one actually runs within its timeout. Defaults to the Claude
# SDK slot count; override with SUMMARIZE_MAX_CONCURRENCY. Per-loop (like the SDK semaphore)
# to avoid binding a global Semaphore to the wrong event loop.
_SUMMARIZE_MAX = int(os.getenv("SUMMARIZE_MAX_CONCURRENCY",
                               os.getenv("CLAUDE_SDK_MAX_CONCURRENCY", "4")))
_SUMMARIZE_SEMAPHORES: "WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = \
    WeakKeyDictionary()


def _summarize_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _SUMMARIZE_SEMAPHORES.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(max(1, _SUMMARIZE_MAX))
        _SUMMARIZE_SEMAPHORES[loop] = sem
    return sem


# Run-level (per thread_id) cache of {url: summary}. A source summarized by one
# researcher is reused if another researcher's search returns the same URL, so popular
# pages (common on a focused topic) are summarized once per run, not once per researcher.
_SUMMARY_CACHE: "dict[str, dict[str, str]]" = {}


def _summary_cache(thread_id) -> dict:
    if not thread_id:
        return {}  # no run id -> a throwaway dict (degrades to no cross-search reuse)
    return _SUMMARY_CACHE.setdefault(str(thread_id), {})


async def _acquire_tavily(queries, n, topic, config) -> dict:
    """Tavily search + dedup -> normalized {url:{url,title,content,raw_content,query}}."""
    search_results = await tavily_search_async(queries, max_results=n, topic=topic,
                                               include_raw_content=True, config=config)
    unique_results = {}
    for response in search_results:
        for result in response["results"]:
            url = result["url"]
            if url not in unique_results:
                unique_results[url] = {**result, "query": response["query"]}
    return unique_results


async def _acquire_exa(queries, n, topic, config) -> dict:
    """Exa neural search + dedup -> normalized {url:{url,title,content,raw_content,query}}.

    Best-effort: any failure (missing key, API/timeout) logs and returns {} so exa_search
    degrades to empty and the hybrid degrades to tavily-only. `topic` is ignored (no Exa equivalent).
    """
    try:
        configurable = Configuration.from_runnable_config(config)
        max_chars = configurable.max_content_length
        exa = Exa(api_key=get_exa_api_key(config))
        timeout_s = float(os.getenv("EXA_TIMEOUT", os.getenv("CLI_BACKEND_TIMEOUT", "120")))
        loop = asyncio.get_running_loop()

        def _one(q):
            return exa.search_and_contents(q, text={"max_characters": max_chars},
                                           summary=True, num_results=n)
        responses = await asyncio.wait_for(
            asyncio.gather(*[loop.run_in_executor(None, _one, q) for q in queries]),
            timeout=timeout_s)
        unique = {}
        for q, resp in zip(queries, responses):
            for r in getattr(resp, "results", []) or []:
                url = getattr(r, "url", "")
                if url and url not in unique:
                    unique[url] = {"url": url, "title": getattr(r, "title", "") or "",
                                   "content": getattr(r, "summary", "") or "",
                                   "raw_content": getattr(r, "text", "") or "",
                                   "query": q}
        return unique
    except Exception as e:  # noqa: BLE001 - best-effort; never break search
        logger.warning("Exa search failed (non-fatal): %s", e)
        return {}


async def _finalize_search(unique_results: dict, config) -> str:
    """Record sources to the factbase, summarize each, format the result string.

    Backend-agnostic: operates on the normalized unique_results dict, so tavily/exa/hybrid
    all feed record_search_sources + summarization identically.
    """
    configurable = Configuration.from_runnable_config(config)
    max_char_to_include = configurable.max_content_length

    # Persist per-source raw text for fact extraction (best-effort; never break search).
    try:
        from open_deep_research.factbase import store as _fb_store
        from open_deep_research.storage import get_db_path as _get_db_path
        import aiosqlite as _aiosqlite
        _configurable = (config or {}).get("configurable", {}) if config else {}
        _thread_id = _configurable.get("thread_id")
        if _thread_id and Configuration.from_runnable_config(config).persist_results:
            async with _aiosqlite.connect(_get_db_path(config)) as _conn:
                from open_deep_research.factbase import migrations as _fbmig, schema as _fbschema
                from open_deep_research.storage import _ensure_schema as _ens
                await _ens(_conn)                       # research_runs must exist before v2 ALTER
                await _fbmig.apply(_conn, _fbschema.STEPS)
                await record_search_sources(_fb_store.RunSourceStore(_conn), str(_thread_id), unique_results)
    except Exception as _e:
        logger.warning("run_source capture failed (non-fatal): %s", _e)

    # Step 3: Set up the summarization model with configuration
    # Character limit to stay within model token limits (configurable)
    # Initialize summarization model with retry logic
    summarization_model = (
        configurable_claude_model()
        .with_structured_output(Summary)
        .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
        .with_config({
            "model": configurable.summarization_model,
            # Bring the highest-volume call (one per source) under the failover chain +
            # ledger like every graph stage -- otherwise a gemini hard-failure here has no
            # Claude backup and is invisible to the failover tracker.
            "model_chain": configurable.model_chain("summarization"),
            "stage": "summarization",
            "max_tokens": configurable.summarization_model_max_tokens,
            "api_key": get_api_key_for_model(configurable.summarization_model, config),
            "tags": ["langsmith:nostream"],
        })
    )

    # Step 4: Produce per-source content. Three call-reducing behaviours:
    #  - summarize_search_results=False -> skip the LLM pass, hand compression the
    #    truncated raw text (or Tavily's snippet); cuts ALL summarize calls.
    #  - run-level cache -> a URL already summarized this run is reused, not re-called.
    #  - the _summarize_semaphore gate keeps in-flight calls matched to model slots.
    do_summarize = configurable.summarize_search_results
    thread_id = (config or {}).get("configurable", {}).get("thread_id") if config else None
    cache = _summary_cache(thread_id)

    async def _summarize_one(result):
        raw = result.get("raw_content")
        url = result.get("url")
        if not do_summarize:
            # Tavily's relevance snippet (short, already extracted) -- no model call;
            # fall back to truncated raw only when no snippet is present.
            return result.get("content") or (raw or "")[:max_char_to_include] or None
        if url and url in cache:
            return cache[url]
        if not raw:
            return None
        async with _summarize_semaphore():
            summary = await summarize_webpage(summarization_model, raw[:max_char_to_include])
        if url:
            cache[url] = summary
        return summary

    summarization_tasks = [_summarize_one(result) for result in unique_results.values()]

    # Step 5: Execute summarization tasks, bounded to _SUMMARIZE_MAX in flight at once.
    # return_exceptions so a single summarization blowing up (beyond summarize_webpage's own
    # fallback) can't sink the whole search tool -- a failed summary falls back to the result's
    # short content.
    summaries = await asyncio.gather(*summarization_tasks, return_exceptions=True)
    summaries = [None if isinstance(s, BaseException) else s for s in summaries]

    # Step 6: Combine results with their summaries
    summarized_results = {
        url: {
            'title': result['title'],
            'content': result['content'] if summary is None else summary
        }
        for url, result, summary in zip(
            unique_results.keys(),
            unique_results.values(),
            summaries
        )
    }

    # Step 7: Format the final output
    if not summarized_results:
        return "No valid search results found. Please try different search queries or use a different search API."

    formatted_output = "Search results: \n\n"
    for i, (url, result) in enumerate(summarized_results.items()):
        formatted_output += f"\n\n--- SOURCE {i+1}: {result['title']} ---\n"
        formatted_output += f"URL: {url}\n\n"
        formatted_output += f"SUMMARY:\n{result['content']}\n\n"
        formatted_output += "\n\n" + "-" * 80 + "\n"

    return formatted_output


async def _acquire_hybrid(queries, n, topic, config) -> dict:
    """Union of tavily + exa: interleave Exa-first, dedup by URL, cap at n. Auto-degrades:
    exa-empty -> tavily-only; tavily-empty -> exa-only."""
    tav, exa = await asyncio.gather(_acquire_tavily(queries, n, topic, config),
                                    _acquire_exa(queries, n, topic, config))
    tav_list, exa_list = list(tav.values()), list(exa.values())
    merged, seen = {}, set()
    for i in range(max(len(tav_list), len(exa_list))):
        for src in (exa_list[i:i+1] + tav_list[i:i+1]):   # exa first
            if src["url"] not in seen and len(merged) < n:
                seen.add(src["url"]); merged[src["url"]] = src
    return merged


EXA_SEARCH_DESCRIPTION = ("Search the web using Exa's neural/semantic search. Returns relevant "
                          "sources with content for research questions.")
@tool(description=EXA_SEARCH_DESCRIPTION)
async def exa_search(queries: List[str],
                     max_results: Annotated[int, InjectedToolArg] = 5,
                     topic: Annotated[Literal["general", "news", "finance"], InjectedToolArg] = "general",
                     config: RunnableConfig = None) -> str:
    """Search the web via Exa (neural) and summarize results."""
    configurable = Configuration.from_runnable_config(config)
    n = min(max_results, configurable.max_search_results)
    return await _finalize_search(await _acquire_exa(queries, n, topic, config), config)


TAVILY_EXA_SEARCH_DESCRIPTION = ("Search the web using BOTH Tavily and Exa, merged for breadth. "
                                 "Best general research option.")
@tool(description=TAVILY_EXA_SEARCH_DESCRIPTION)
async def tavily_exa_search(queries: List[str],
                            max_results: Annotated[int, InjectedToolArg] = 5,
                            topic: Annotated[Literal["general", "news", "finance"], InjectedToolArg] = "general",
                            config: RunnableConfig = None) -> str:
    """Search via Tavily + Exa (interleaved, deduped, capped) and summarize."""
    configurable = Configuration.from_runnable_config(config)
    n = min(max_results, configurable.max_search_results)
    return await _finalize_search(await _acquire_hybrid(queries, n, topic, config), config)


@tool(description=TAVILY_SEARCH_DESCRIPTION)
async def tavily_search(
    queries: List[str],
    max_results: Annotated[int, InjectedToolArg] = 5,
    topic: Annotated[Literal["general", "news", "finance"], InjectedToolArg] = "general",
    config: RunnableConfig = None
) -> str:
    """Fetch and summarize search results from Tavily search API."""
    configurable = Configuration.from_runnable_config(config)
    n = min(max_results, configurable.max_search_results)
    logger.info("Tavily search executing for queries: %s (max_results=%d)", queries, n)
    unique_results = await _acquire_tavily(queries, n, topic, config)
    logger.info("Tavily found %d unique results for %d queries", len(unique_results), len(queries))
    return await _finalize_search(unique_results, config)


async def tavily_search_async(
    search_queries, 
    max_results: int = 5, 
    topic: Literal["general", "news", "finance"] = "general", 
    include_raw_content: bool = True, 
    config: RunnableConfig = None
):
    """Execute multiple Tavily search queries asynchronously.
    
    Args:
        search_queries: List of search query strings to execute
        max_results: Maximum number of results per query
        topic: Topic category for filtering results
        include_raw_content: Whether to include full webpage content
        config: Runtime configuration for API key access
        
    Returns:
        List of search result dictionaries from Tavily API
    """
    # Initialize the Tavily client with API key from config
    tavily_client = AsyncTavilyClient(api_key=get_tavily_api_key(config))

    # Bound each call: the Tavily HTTP client has no timeout by default, so a hung
    # endpoint would block indefinitely (the only unbounded call among the backends).
    # Share the CLI backends' timeout budget for consistency.
    timeout_s = float(os.getenv("TAVILY_TIMEOUT", os.getenv("CLI_BACKEND_TIMEOUT", "120")))

    # Create search tasks for parallel execution
    search_tasks = [
        asyncio.wait_for(
            tavily_client.search(
                query,
                max_results=max_results,
                include_raw_content=include_raw_content,
                topic=topic
            ),
            timeout=timeout_s,
        )
        for query in search_queries
    ]

    # Execute all queries in parallel. return_exceptions so one hung/failed query
    # doesn't sink the batch; drop failures (logged) and keep the successful responses.
    raw = await asyncio.gather(*search_tasks, return_exceptions=True)
    search_results = []
    for query, result in zip(search_queries, raw):
        if isinstance(result, BaseException):
            logger.error("Tavily search failed for query %r: %s", query, result, exc_info=result)
            continue
        search_results.append(result)
    return search_results

async def summarize_webpage(model: BaseChatModel, webpage_content: str) -> str:
    """Summarize webpage content using AI model with timeout protection.
    
    Args:
        model: The chat model configured for summarization
        webpage_content: Raw webpage content to be summarized
        
    Returns:
        Formatted summary with key excerpts, or original content if summarization fails
    """
    # Per-source summarize cap. Tunable via SUMMARIZE_TIMEOUT_S (default 120s); lower
    # it to stop a slow/throttled backend from spending up to 2 minutes per source on
    # large fan-outs (the dominant runtime cost observed on throttled-Claude runs).
    summarize_timeout_s = float(os.getenv("SUMMARIZE_TIMEOUT_S", "120"))
    try:
        # Create prompt with current date context
        prompt_content = summarize_webpage_prompt.format(
            webpage_content=webpage_content,
            date=get_today_str()
        )

        # Execute summarization with timeout to prevent hanging
        summary = await asyncio.wait_for(
            model.ainvoke([HumanMessage(content=prompt_content)]),
            timeout=summarize_timeout_s
        )

        # Format the summary with structured sections
        formatted_summary = (
            f"<summary>\n{summary.summary}\n</summary>\n\n"
            f"<key_excerpts>\n{summary.key_excerpts}\n</key_excerpts>"
        )

        return formatted_summary

    except asyncio.TimeoutError:
        # Timeout during summarization - return original content
        logging.warning("Summarization timed out after %.0f seconds, returning original content",
                        summarize_timeout_s)
        return webpage_content
    except Exception as e:
        # Other errors during summarization - log and return original content
        logging.warning(f"Summarization failed with error: {str(e)}, returning original content")
        return webpage_content

##########################
# Reflection Tool Utils
##########################

@tool(description="Strategic reflection tool for research planning")
def think_tool(reflection: str) -> str:
    """Tool for strategic reflection on research progress and decision-making.

    Use this tool after each search to analyze results and plan next steps systematically.
    This creates a deliberate pause in the research workflow for quality decision-making.

    When to use:
    - After receiving search results: What key information did I find?
    - Before deciding next steps: Do I have enough to answer comprehensively?
    - When assessing research gaps: What specific information am I still missing?
    - Before concluding research: Can I provide a complete answer now?

    Reflection should address:
    1. Analysis of current findings - What concrete information have I gathered?
    2. Gap assessment - What crucial information is still missing?
    3. Quality evaluation - Do I have sufficient evidence/examples for a good answer?
    4. Strategic decision - Should I continue searching or provide my answer?

    Args:
        reflection: Your detailed reflection on research progress, findings, gaps, and next steps

    Returns:
        Confirmation that reflection was recorded for decision-making
    """
    return f"Reflection recorded: {reflection}"

##########################
# MCP Utils
##########################

async def get_mcp_access_token(
    supabase_token: str,
    base_mcp_url: str,
) -> Optional[Dict[str, Any]]:
    """Exchange Supabase token for MCP access token using OAuth token exchange.
    
    Args:
        supabase_token: Valid Supabase authentication token
        base_mcp_url: Base URL of the MCP server
        
    Returns:
        Token data dictionary if successful, None if failed
    """
    try:
        # Prepare OAuth token exchange request data
        form_data = {
            "client_id": "mcp_default",
            "subject_token": supabase_token,
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "resource": base_mcp_url.rstrip("/") + "/mcp",
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        }
        
        # Execute token exchange request
        async with aiohttp.ClientSession() as session:
            token_url = base_mcp_url.rstrip("/") + "/oauth/token"
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            
            async with session.post(token_url, headers=headers, data=form_data) as response:
                if response.status == 200:
                    # Successfully obtained token
                    token_data = await response.json()
                    return token_data
                else:
                    # Log error details for debugging
                    response_text = await response.text()
                    logging.error(f"Token exchange failed: {response_text}")
                    
    except Exception as e:
        logging.error(f"Error during token exchange: {e}")
    
    return None

async def get_tokens(config: RunnableConfig):
    """Retrieve stored authentication tokens with expiration validation.
    
    Args:
        config: Runtime configuration containing thread and user identifiers
        
    Returns:
        Token dictionary if valid and not expired, None otherwise
    """
    store = get_store()
    
    # Extract required identifiers from config
    thread_id = config.get("configurable", {}).get("thread_id")
    if not thread_id:
        return None
        
    user_id = config.get("metadata", {}).get("owner")
    if not user_id:
        return None
    
    # Retrieve stored tokens
    tokens = await store.aget((user_id, "tokens"), "data")
    if not tokens:
        return None
    
    # Check token expiration
    expires_in = tokens.value.get("expires_in")  # seconds until expiration
    created_at = tokens.created_at  # datetime of token creation
    current_time = datetime.now(timezone.utc)
    expiration_time = created_at + timedelta(seconds=expires_in)
    
    if current_time > expiration_time:
        # Token expired, clean up and return None
        await store.adelete((user_id, "tokens"), "data")
        return None

    return tokens.value

async def set_tokens(config: RunnableConfig, tokens: dict[str, Any]):
    """Store authentication tokens in the configuration store.
    
    Args:
        config: Runtime configuration containing thread and user identifiers
        tokens: Token dictionary to store
    """
    store = get_store()
    
    # Extract required identifiers from config
    thread_id = config.get("configurable", {}).get("thread_id")
    if not thread_id:
        return
        
    user_id = config.get("metadata", {}).get("owner")
    if not user_id:
        return
    
    # Store the tokens
    await store.aput((user_id, "tokens"), "data", tokens)

async def fetch_tokens(config: RunnableConfig) -> dict[str, Any]:
    """Fetch and refresh MCP tokens, obtaining new ones if needed.
    
    Args:
        config: Runtime configuration with authentication details
        
    Returns:
        Valid token dictionary, or None if unable to obtain tokens
    """
    # Try to get existing valid tokens first
    current_tokens = await get_tokens(config)
    if current_tokens:
        return current_tokens
    
    # Extract Supabase token for new token exchange
    supabase_token = config.get("configurable", {}).get("x-supabase-access-token")
    if not supabase_token:
        return None
    
    # Extract MCP configuration
    mcp_config = config.get("configurable", {}).get("mcp_config")
    if not mcp_config or not mcp_config.get("url"):
        return None
    
    # Exchange Supabase token for MCP tokens
    mcp_tokens = await get_mcp_access_token(supabase_token, mcp_config.get("url"))
    if not mcp_tokens:
        return None

    # Store the new tokens and return them
    await set_tokens(config, mcp_tokens)
    return mcp_tokens

def wrap_mcp_authenticate_tool(tool: StructuredTool) -> StructuredTool:
    """Wrap MCP tool with comprehensive authentication and error handling.
    
    Args:
        tool: The MCP structured tool to wrap
        
    Returns:
        Enhanced tool with authentication error handling
    """
    original_coroutine = tool.coroutine
    
    async def authentication_wrapper(**kwargs):
        """Enhanced coroutine with MCP error handling and user-friendly messages."""
        
        def _find_mcp_error_in_exception_chain(exc: BaseException) -> McpError | None:
            """Recursively search for MCP errors in exception chains."""
            if isinstance(exc, McpError):
                return exc
            
            # Handle ExceptionGroup (Python 3.11+) by checking attributes
            if hasattr(exc, 'exceptions'):
                for sub_exception in exc.exceptions:
                    if found_error := _find_mcp_error_in_exception_chain(sub_exception):
                        return found_error
            return None
        
        try:
            # Execute the original tool functionality
            return await original_coroutine(**kwargs)
            
        except BaseException as original_error:
            # Search for MCP-specific errors in the exception chain
            mcp_error = _find_mcp_error_in_exception_chain(original_error)
            if not mcp_error:
                # Not an MCP error, re-raise the original exception
                raise original_error
            
            # Handle MCP-specific error cases
            error_details = mcp_error.error
            error_code = getattr(error_details, "code", None)
            error_data = getattr(error_details, "data", None) or {}
            
            # Check for authentication/interaction required error
            if error_code == -32003:  # Interaction required error code
                message_payload = error_data.get("message", {})
                error_message = "Required interaction"
                
                # Extract user-friendly message if available
                if isinstance(message_payload, dict):
                    error_message = message_payload.get("text") or error_message
                
                # Append URL if provided for user reference
                if url := error_data.get("url"):
                    error_message = f"{error_message} {url}"
                
                raise ToolException(error_message) from original_error
            
            # For other MCP errors, re-raise the original
            raise original_error
    
    # Replace the tool's coroutine with our enhanced version
    tool.coroutine = authentication_wrapper
    return tool

async def load_mcp_tools(
    config: RunnableConfig,
    existing_tool_names: set[str],
) -> list[BaseTool]:
    """Load and configure MCP (Model Context Protocol) tools with authentication.
    
    Args:
        config: Runtime configuration containing MCP server details
        existing_tool_names: Set of tool names already in use to avoid conflicts
        
    Returns:
        List of configured MCP tools ready for use
    """
    configurable = Configuration.from_runnable_config(config)
    
    # Step 1: Handle authentication if required
    if configurable.mcp_config and configurable.mcp_config.auth_required:
        mcp_tokens = await fetch_tokens(config)
    else:
        mcp_tokens = None
    
    # Step 2: Validate configuration requirements
    config_valid = (
        configurable.mcp_config and 
        configurable.mcp_config.url and 
        configurable.mcp_config.tools and 
        (mcp_tokens or not configurable.mcp_config.auth_required)
    )
    
    if not config_valid:
        return []
    
    # Step 3: Set up MCP server connection
    server_url = configurable.mcp_config.url.rstrip("/") + "/mcp"
    
    # Configure authentication headers if tokens are available
    auth_headers = None
    if mcp_tokens:
        auth_headers = {"Authorization": f"Bearer {mcp_tokens['access_token']}"}
    
    mcp_server_config = {
        "server_1": {
            "url": server_url,
            "headers": auth_headers,
            "transport": "streamable_http"
        }
    }
    # TODO: When Multi-MCP Server support is merged in OAP, update this code
    
    # Step 4: Load tools from MCP server
    try:
        client = MultiServerMCPClient(mcp_server_config)
        available_mcp_tools = await client.get_tools()
    except Exception as e:
        # MCP unreachable: proceed without MCP tools, but log it -- otherwise a configured
        # MCP server being down silently drops its tools (or surfaces later as a confusing
        # "no tools found" error if MCP was the only source).
        logger.warning("MCP tool load failed; proceeding without MCP tools: %s", e, exc_info=True)
        return []
    
    # Step 5: Filter and configure tools
    configured_tools = []
    for mcp_tool in available_mcp_tools:
        # Skip tools with conflicting names
        if mcp_tool.name in existing_tool_names:
            warnings.warn(
                f"MCP tool '{mcp_tool.name}' conflicts with existing tool name - skipping"
            )
            continue
        
        # Only include tools specified in configuration
        if mcp_tool.name not in set(configurable.mcp_config.tools):
            continue
        
        # Wrap tool with authentication handling and add to list
        enhanced_tool = wrap_mcp_authenticate_tool(mcp_tool)
        configured_tools.append(enhanced_tool)
    
    return configured_tools


##########################
# Claude Code Web Search Tool
##########################
CLAUDE_SEARCH_DESCRIPTION = (
    "Search the web using Claude Code's native web search. Useful for when you need "
    "to answer questions about current events or find up-to-date information online."
)
@tool(description=CLAUDE_SEARCH_DESCRIPTION)
async def claude_web_search(
    queries: List[str],
    max_results: Annotated[int, InjectedToolArg] = 5,
    topic: Annotated[Literal["general", "news", "finance"], InjectedToolArg] = "general",
    config: RunnableConfig = None
) -> str:
    """Search the web via Claude Code's built-in WebSearch/WebFetch tools.

    The model supplies the queries; Claude Code performs the actual searching and
    returns compiled findings with source URLs (no separate summarization pass or
    third-party search key required).

    Args:
        queries: List of search queries to execute
        max_results: Maximum number of results to consider per query
        topic: Topic hint for the search (general, news, or finance)
        config: Runtime configuration for model selection

    Returns:
        Formatted string containing search findings with sources
    """
    configurable = Configuration.from_runnable_config(config)
    search_model = to_claude_model(configurable.summarization_model)
    return await run_search_agent(queries, model=search_model, max_results=max_results)


GEMINI_SEARCH_DESCRIPTION = (
    "Search the web using Gemini's built-in Google Search. Useful for when you need "
    "to answer questions about current events or find up-to-date information online."
)
@tool(description=GEMINI_SEARCH_DESCRIPTION)
async def gemini_web_search(
    queries: List[str],
    max_results: Annotated[int, InjectedToolArg] = 5,
    topic: Annotated[Literal["general", "news", "finance"], InjectedToolArg] = "general",
    config: RunnableConfig = None
) -> str:
    """Search the web via the Gemini CLI's Google Search grounding."""
    configurable = Configuration.from_runnable_config(config)
    search_model = to_gemini_model(configurable.summarization_model)
    return await run_gemini_search(queries, model=search_model, max_results=max_results)


CODEX_SEARCH_DESCRIPTION = (
    "Search the web using Codex's web search tool. Useful for when you need to "
    "answer questions about current events or find up-to-date information online."
)
@tool(description=CODEX_SEARCH_DESCRIPTION)
async def codex_web_search(
    queries: List[str],
    max_results: Annotated[int, InjectedToolArg] = 5,
    topic: Annotated[Literal["general", "news", "finance"], InjectedToolArg] = "general",
    config: RunnableConfig = None
) -> str:
    """Search the web via the Codex CLI's web search tool."""
    configurable = Configuration.from_runnable_config(config)
    search_model = to_codex_model(configurable.summarization_model)
    return await run_codex_search(queries, model=search_model, max_results=max_results)


##########################
# Tool Utils
##########################

async def get_search_tool(search_api: SearchAPI):
    """Configure and return search tools based on the specified API provider.
    
    Args:
        search_api: The search API provider to use (Anthropic, OpenAI, Tavily, or None)
        
    Returns:
        List of configured search tool objects for the specified provider
    """
    if search_api in (SearchAPI.CLAUDE, SearchAPI.GEMINI, SearchAPI.CODEX):
        # CLI-backed web search (executed by the graph via the tool).
        search_tool = {
            SearchAPI.CLAUDE: claude_web_search,
            SearchAPI.GEMINI: gemini_web_search,
            SearchAPI.CODEX: codex_web_search,
        }[search_api]
        search_tool.metadata = {
            **(search_tool.metadata or {}),
            "type": "search",
            "name": "web_search"
        }
        return [search_tool]

    elif search_api == SearchAPI.EXA:
        # Exa neural search
        search_tool = exa_search
        search_tool.metadata = {
            **(search_tool.metadata or {}),
            "type": "search",
            "name": "web_search"
        }
        return [search_tool]

    elif search_api == SearchAPI.TAVILY_EXA:
        # Hybrid Tavily + Exa search
        search_tool = tavily_exa_search
        search_tool.metadata = {
            **(search_tool.metadata or {}),
            "type": "search",
            "name": "web_search"
        }
        return [search_tool]

    elif search_api == SearchAPI.ANTHROPIC:
        # Anthropic's native web search with usage limits
        return [{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 5
        }]

    elif search_api == SearchAPI.OPENAI:
        # OpenAI's web search preview functionality
        return [{"type": "web_search_preview"}]

    elif search_api == SearchAPI.TAVILY:
        # Configure Tavily search tool with metadata
        search_tool = tavily_search
        search_tool.metadata = {
            **(search_tool.metadata or {}),
            "type": "search",
            "name": "web_search"
        }
        return [search_tool]
        
    elif search_api == SearchAPI.NONE:
        # No search functionality configured
        return []
        
    # Default fallback for unknown search API types
    return []
    
async def get_all_tools(config: RunnableConfig):
    """Assemble complete toolkit including research, search, and MCP tools.
    
    Args:
        config: Runtime configuration specifying search API and MCP settings
        
    Returns:
        List of all configured and available tools for research operations
    """
    # Start with core research tools
    tools = [tool(ResearchComplete), think_tool]
    
    # Add configured search tools
    configurable = Configuration.from_runnable_config(config)
    search_api = SearchAPI(get_config_value(configurable.search_api))
    search_tools = await get_search_tool(search_api)
    tools.extend(search_tools)
    
    # Track existing tool names to prevent conflicts
    existing_tool_names = {
        tool.name if hasattr(tool, "name") else tool.get("name", "web_search") 
        for tool in tools
    }
    
    # Add MCP tools if configured
    mcp_tools = await load_mcp_tools(config, existing_tool_names)
    tools.extend(mcp_tools)
    
    return tools

def get_notes_from_tool_calls(messages: list[MessageLikeRepresentation]):
    """Extract notes from tool call messages."""
    return [tool_msg.content for tool_msg in filter_messages(messages, include_types="tool")]

##########################
# Model Provider Native Websearch Utils
##########################

def anthropic_websearch_called(response):
    """Detect if Anthropic's native web search was used in the response.
    
    Args:
        response: The response object from Anthropic's API
        
    Returns:
        True if web search was called, False otherwise
    """
    try:
        # Navigate through the response metadata structure
        usage = response.response_metadata.get("usage")
        if not usage:
            return False
        
        # Check for server-side tool usage information
        server_tool_use = usage.get("server_tool_use")
        if not server_tool_use:
            return False
        
        # Look for web search request count
        web_search_requests = server_tool_use.get("web_search_requests")
        if web_search_requests is None:
            return False
        
        # Return True if any web search requests were made
        return web_search_requests > 0
        
    except (AttributeError, TypeError):
        # Handle cases where response structure is unexpected
        return False

def openai_websearch_called(response):
    """Detect if OpenAI's web search functionality was used in the response.
    
    Args:
        response: The response object from OpenAI's API
        
    Returns:
        True if web search was called, False otherwise
    """
    # Check for tool outputs in the response metadata
    tool_outputs = response.additional_kwargs.get("tool_outputs")
    if not tool_outputs:
        return False
    
    # Look for web search calls in the tool outputs
    for tool_output in tool_outputs:
        if tool_output.get("type") == "web_search_call":
            return True
    
    return False


##########################
# Token Limit Exceeded Utils
##########################

def is_token_limit_exceeded(exception: Exception, model_name: str = None) -> bool:
    """Determine if an exception indicates a token/context limit was exceeded.
    
    Args:
        exception: The exception to analyze
        model_name: Optional model name to optimize provider detection
        
    Returns:
        True if the exception indicates a token limit was exceeded, False otherwise
    """
    error_str = str(exception).lower()
    
    # Step 1: Determine provider from model name if available
    provider = None
    if model_name:
        model_str = str(model_name).lower()
        if model_str.startswith('openai:'):
            provider = 'openai'
        elif model_str.startswith('anthropic:'):
            provider = 'anthropic'
        elif model_str.startswith('gemini:') or model_str.startswith('google:'):
            provider = 'gemini'
    
    # Step 2: Check provider-specific token limit patterns
    if provider == 'openai':
        return _check_openai_token_limit(exception, error_str)
    elif provider == 'anthropic':
        return _check_anthropic_token_limit(exception, error_str)
    elif provider == 'gemini':
        return _check_gemini_token_limit(exception, error_str)
    
    # Step 3: If provider unknown, check all providers, plus a provider-agnostic text
    # check. The CLI/subscription backends (ClaudeAgentChat/Gemini/Codex) raise plain
    # RuntimeError/ValueError whose class+module don't match the provider SDKs, so the
    # checks above miss a genuine context-overflow on the backends this fork actually
    # runs -- match the common overflow phrasings directly.
    return (
        _check_openai_token_limit(exception, error_str) or
        _check_anthropic_token_limit(exception, error_str) or
        _check_gemini_token_limit(exception, error_str) or
        _check_generic_token_limit(error_str)
    )


def _check_generic_token_limit(error_str: str) -> bool:
    """Provider-agnostic context/token-overflow detection from the error text."""
    markers = (
        "context length", "context window", "context_length_exceeded",
        "maximum context", "too many tokens", "token limit", "max_tokens",
        "prompt is too long", "input is too long", "exceeds the limit",
        "reduce the length", "too long",
    )
    return any(m in error_str for m in markers)

def _check_openai_token_limit(exception: Exception, error_str: str) -> bool:
    """Check if exception indicates OpenAI token limit exceeded."""
    # Analyze exception metadata
    exception_type = str(type(exception))
    class_name = exception.__class__.__name__
    module_name = getattr(exception.__class__, '__module__', '')
    
    # Check if this is an OpenAI exception
    is_openai_exception = (
        'openai' in exception_type.lower() or 
        'openai' in module_name.lower()
    )
    
    # Check for typical OpenAI token limit error types
    is_request_error = class_name in ['BadRequestError', 'InvalidRequestError']
    
    if is_openai_exception and is_request_error:
        # Look for token-related keywords in error message
        token_keywords = ['token', 'context', 'length', 'maximum context', 'reduce']
        if any(keyword in error_str for keyword in token_keywords):
            return True
    
    # Check for specific OpenAI error codes
    if hasattr(exception, 'code') and hasattr(exception, 'type'):
        error_code = getattr(exception, 'code', '')
        error_type = getattr(exception, 'type', '')
        
        if (error_code == 'context_length_exceeded' or
            error_type == 'invalid_request_error'):
            return True
    
    return False

def _check_anthropic_token_limit(exception: Exception, error_str: str) -> bool:
    """Check if exception indicates Anthropic token limit exceeded."""
    # Analyze exception metadata
    exception_type = str(type(exception))
    class_name = exception.__class__.__name__
    module_name = getattr(exception.__class__, '__module__', '')
    
    # Check if this is an Anthropic exception
    is_anthropic_exception = (
        'anthropic' in exception_type.lower() or 
        'anthropic' in module_name.lower()
    )
    
    # Check for Anthropic-specific error patterns
    is_bad_request = class_name == 'BadRequestError'
    
    if is_anthropic_exception and is_bad_request:
        # Anthropic uses specific error messages for token limits
        if 'prompt is too long' in error_str:
            return True
    
    return False

def _check_gemini_token_limit(exception: Exception, error_str: str) -> bool:
    """Check if exception indicates Google/Gemini token limit exceeded."""
    # Analyze exception metadata
    exception_type = str(type(exception))
    class_name = exception.__class__.__name__
    module_name = getattr(exception.__class__, '__module__', '')
    
    # Check if this is a Google/Gemini exception
    is_google_exception = (
        'google' in exception_type.lower() or 
        'google' in module_name.lower()
    )
    
    # Check for Google-specific resource exhaustion errors
    is_resource_exhausted = class_name in [
        'ResourceExhausted', 
        'GoogleGenerativeAIFetchError'
    ]
    
    if is_google_exception and is_resource_exhausted:
        return True
    
    # Check for specific Google API resource exhaustion patterns
    if 'google.api_core.exceptions.resourceexhausted' in exception_type.lower():
        return True
    
    return False

# NOTE: This may be out of date or not applicable to your models. Please update this as needed.
MODEL_TOKEN_LIMITS = {
    "openai:gpt-4.1-mini": 1047576,
    "openai:gpt-4.1-nano": 1047576,
    "openai:gpt-4.1": 1047576,
    "openai:gpt-4o-mini": 128000,
    "openai:gpt-4o": 128000,
    "openai:o4-mini": 200000,
    "openai:o3-mini": 200000,
    "openai:o3": 200000,
    "openai:o3-pro": 200000,
    "openai:o1": 200000,
    "openai:o1-pro": 200000,
    "anthropic:claude-opus-4": 200000,
    "anthropic:claude-sonnet-4": 200000,
    "anthropic:claude-3-7-sonnet": 200000,
    "anthropic:claude-3-5-sonnet": 200000,
    "anthropic:claude-3-5-haiku": 200000,
    "google:gemini-1.5-pro": 2097152,
    "google:gemini-1.5-flash": 1048576,
    "google:gemini-pro": 32768,
    "cohere:command-r-plus": 128000,
    "cohere:command-r": 128000,
    "cohere:command-light": 4096,
    "cohere:command": 4096,
    "mistral:mistral-large": 32768,
    "mistral:mistral-medium": 32768,
    "mistral:mistral-small": 32768,
    "mistral:mistral-7b-instruct": 32768,
    "ollama:codellama": 16384,
    "ollama:llama2:70b": 4096,
    "ollama:llama2:13b": 4096,
    "ollama:llama2": 4096,
    "ollama:mistral": 32768,
    "bedrock:us.amazon.nova-premier-v1:0": 1000000,
    "bedrock:us.amazon.nova-pro-v1:0": 300000,
    "bedrock:us.amazon.nova-lite-v1:0": 300000,
    "bedrock:us.amazon.nova-micro-v1:0": 128000,
    "bedrock:us.anthropic.claude-3-7-sonnet-20250219-v1:0": 200000,
    "bedrock:us.anthropic.claude-sonnet-4-20250514-v1:0": 200000,
    "bedrock:us.anthropic.claude-opus-4-20250514-v1:0": 200000,
    "anthropic.claude-opus-4-1-20250805-v1:0": 200000,
}

def get_model_token_limit(model_string):
    """Look up the token limit for a specific model.
    
    Args:
        model_string: The model identifier string to look up
        
    Returns:
        Token limit as integer if found, None if model not in lookup table
    """
    # Search through known model token limits
    for model_key, token_limit in MODEL_TOKEN_LIMITS.items():
        if model_key in model_string:
            return token_limit
    
    # Model not found in lookup table
    return None

def remove_up_to_last_ai_message(messages: list[MessageLikeRepresentation]) -> list[MessageLikeRepresentation]:
    """Truncate message history by removing up to the last AI message.
    
    This is useful for handling token limit exceeded errors by removing recent context.
    
    Args:
        messages: List of message objects to truncate
        
    Returns:
        Truncated message list up to (but not including) the last AI message
    """
    # Search backwards through messages to find the last AI message
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], AIMessage):
            # Return everything up to (but not including) the last AI message
            return messages[:i]
    
    # No AI messages found, return original list
    return messages

##########################
# Misc Utils
##########################

def get_today_str() -> str:
    """Get current date formatted for display in prompts and outputs.
    
    Returns:
        Human-readable date string in format like 'Mon Jan 15, 2024'
    """
    now = datetime.now()
    return f"{now:%a} {now:%b} {now.day}, {now:%Y}"

def get_config_value(value):
    """Extract value from configuration, handling enums and None values."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    elif isinstance(value, dict):
        return value
    else:
        return value.value

def get_api_key_for_model(model_name: str, config: RunnableConfig):
    """Get API key for a specific model from environment or config."""
    # In Claude subscription mode all LLM calls go through the Claude Agent SDK,
    # which authenticates via the logged-in Claude Code CLI. Returning no key keeps
    # ANTHROPIC_API_KEY out of the path so usage bills against the subscription.
    if use_subscription():
        return None
    should_get_from_config = os.getenv("GET_API_KEYS_FROM_CONFIG", "false")
    model_name = model_name.lower()
    if should_get_from_config.lower() == "true":
        api_keys = config.get("configurable", {}).get("apiKeys", {})
        if not api_keys:
            return None
        if model_name.startswith("openai:"):
            return api_keys.get("OPENAI_API_KEY")
        elif model_name.startswith("anthropic:"):
            return api_keys.get("ANTHROPIC_API_KEY")
        elif model_name.startswith("google"):
            return api_keys.get("GOOGLE_API_KEY")
        return None
    else:
        if model_name.startswith("openai:"): 
            return os.getenv("OPENAI_API_KEY")
        elif model_name.startswith("anthropic:"):
            return os.getenv("ANTHROPIC_API_KEY")
        elif model_name.startswith("google"):
            return os.getenv("GOOGLE_API_KEY")
        return None

def get_tavily_api_key(config: RunnableConfig):
    """Get Tavily API key from environment or config."""
    should_get_from_config = os.getenv("GET_API_KEYS_FROM_CONFIG", "false")
    if should_get_from_config.lower() == "true":
        api_keys = config.get("configurable", {}).get("apiKeys", {})
        if not api_keys:
            return None
        return api_keys.get("TAVILY_API_KEY")
    else:
        return os.getenv("TAVILY_API_KEY")


def get_exa_api_key(config: RunnableConfig):
    """Get Exa API key from environment or config (mirrors get_tavily_api_key precedence)."""
    should_get_from_config = os.getenv("GET_API_KEYS_FROM_CONFIG", "false")
    if should_get_from_config.lower() == "true":
        api_keys = config.get("configurable", {}).get("apiKeys", {})
        if not api_keys:
            return None
        return api_keys.get("EXA_API_KEY")
    else:
        return os.getenv("EXA_API_KEY")
