"""Researcher node, tool execution, compression, and the researcher subgraph."""

import asyncio
import logging
from typing import Literal

from langchain_core.messages import (
    HumanMessage,
    SystemMessage,
    ToolMessage,
    filter_messages,
)
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from open_deep_research.claude_agent_chat import configurable_claude_model
from open_deep_research.configuration import Configuration
from open_deep_research.nodes.common import COMPRESSION_FAILED_SENTINEL
from open_deep_research.prompts import (
    compress_research_simple_human_message,
    compress_research_system_prompt,
    research_system_prompt,
)
from open_deep_research.state import ResearcherOutputState, ResearcherState
from open_deep_research.utils import (
    anthropic_websearch_called,
    get_all_tools,
    get_api_key_for_model,
    get_today_str,
    is_token_limit_exceeded,
    openai_websearch_called,
    remove_up_to_last_ai_message,
)

logger = logging.getLogger(__name__)

# Initialize a configurable model for the researcher nodes.
configurable_model = configurable_claude_model(
    default_config={"model": "gemini:gemini-2.5-flash"}
)

async def researcher(state: ResearcherState, config: RunnableConfig) -> Command[Literal["researcher_tools"]]:
    """Individual researcher that conducts focused research on specific topics.

    This researcher is given a specific research topic by the supervisor and uses
    available tools (search, think_tool, MCP tools) to gather comprehensive information.
    It can use think_tool for strategic planning between searches.

    Args:
        state: Current researcher state with messages and topic context
        config: Runtime configuration with model settings and tool availability

    Returns:
        Command to proceed to researcher_tools for tool execution
    """
    # Step 1: Load configuration and validate tool availability
    configurable = Configuration.from_runnable_config(config)
    researcher_messages = state.get("researcher_messages", [])

    # Get all available research tools (search, MCP, think_tool)
    tools = await get_all_tools(config)
    if len(tools) == 0:
        raise ValueError(
            "No tools found to conduct research: Please configure either your "
            "search API or add MCP tools to your configuration."
        )

    # Step 2: Configure the researcher model with tools
    researcher_model_config = {
        "model": configurable.researcher_model,
        "model_chain": configurable.model_chain("researcher"),
        "stage": "researcher",
        "max_tokens": configurable.researcher_model_max_tokens,
        "api_key": get_api_key_for_model(configurable.researcher_model, config),
        "tags": ["langsmith:nostream"]
    }

    # Prepare system prompt with MCP context if available
    researcher_prompt = research_system_prompt.format(
        mcp_prompt=configurable.mcp_prompt or "",
        date=get_today_str()
    )

    # Configure model with tools, retry logic, and settings
    researcher_model = (
        configurable_model
        .bind_tools(tools)
        .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
        .with_config(researcher_model_config)
    )

    # Step 3: Generate researcher response with system context
    messages = [SystemMessage(content=researcher_prompt)] + researcher_messages
    response = await researcher_model.ainvoke(messages)

    # Step 4: Update state and proceed to tool execution
    return Command(
        goto="researcher_tools",
        update={
            "researcher_messages": [response],
            "tool_call_iterations": state.get("tool_call_iterations", 0) + 1
        }
    )

# Tool Execution Helper Function
async def execute_tool_safely(tool, args, config):
    """Safely execute a tool with error handling.

    The error string is returned to the researcher LLM (so it can adapt), but it is
    ALSO logged at error level: otherwise a systemic failure (dead search key, down MCP)
    makes every tool "fail" invisibly while the run still completes with a hollow report.
    """
    try:
        return await tool.ainvoke(args, config)
    except Exception as e:
        tool_name = getattr(tool, "name", None) or getattr(tool, "__name__", "unknown")
        logger.error("Tool %r execution failed: %s", tool_name, e, exc_info=True)
        return f"Error executing tool: {str(e)}"


async def researcher_tools(state: ResearcherState, config: RunnableConfig) -> Command[Literal["researcher", "compress_research"]]:
    """Execute tools called by the researcher, including search tools and strategic thinking.

    This function handles various types of researcher tool calls:
    1. think_tool - Strategic reflection that continues the research conversation
    2. Search tools (tavily_search, web_search) - Information gathering
    3. MCP tools - External tool integrations
    4. ResearchComplete - Signals completion of individual research task

    Args:
        state: Current researcher state with messages and iteration count
        config: Runtime configuration with research limits and tool settings

    Returns:
        Command to either continue research loop or proceed to compression
    """
    # Step 1: Extract current state and check early exit conditions
    configurable = Configuration.from_runnable_config(config)
    researcher_messages = state.get("researcher_messages", [])
    most_recent_message = researcher_messages[-1]

    # Early exit if no tool calls were made (including native web search)
    has_tool_calls = bool(most_recent_message.tool_calls)
    has_native_search = (
        openai_websearch_called(most_recent_message) or
        anthropic_websearch_called(most_recent_message)
    )

    if not has_tool_calls and not has_native_search:
        return Command(goto="compress_research")

    # Step 2: Handle other tool calls (search, MCP tools, etc.)
    tools = await get_all_tools(config)
    tools_by_name = {
        tool.name if hasattr(tool, "name") else tool.get("name", "web_search"): tool
        for tool in tools
    }

    # Execute all tool calls in parallel. Guard unknown tool names: the CLI/subscription
    # backends select tools by name with no enum enforcement, so a hallucinated/mis-typed
    # name would KeyError here and crash the whole researcher unit. Answer those with a
    # corrective ToolMessage (and log) instead, so the researcher can retry a valid tool.
    tool_calls = most_recent_message.tool_calls
    valid_tool_calls = []
    unknown_tool_outputs = []
    for tool_call in tool_calls:
        tool = tools_by_name.get(tool_call["name"])
        if tool is None:
            logger.warning(
                "Researcher requested unknown tool %r (available: %s)",
                tool_call["name"], sorted(tools_by_name),
            )
            unknown_tool_outputs.append(ToolMessage(
                content=(
                    f"Error: '{tool_call['name']}' is not an available tool. "
                    f"Choose one of: {sorted(tools_by_name)}."
                ),
                name=tool_call["name"],
                tool_call_id=tool_call["id"],
            ))
            continue
        valid_tool_calls.append(tool_call)

    tool_execution_tasks = [
        execute_tool_safely(tools_by_name[tool_call["name"]], tool_call["args"], config)
        for tool_call in valid_tool_calls
    ]
    observations = await asyncio.gather(*tool_execution_tasks)

    # Create tool messages from execution results (valid runs + any unknown-tool nudges)
    tool_outputs = [
        ToolMessage(
            content=observation,
            name=tool_call["name"],
            tool_call_id=tool_call["id"]
        )
        for observation, tool_call in zip(observations, valid_tool_calls)
    ]
    tool_outputs.extend(unknown_tool_outputs)

    # Step 3: Check late exit conditions (after processing tools)
    exceeded_iterations = state.get("tool_call_iterations", 0) >= configurable.max_react_tool_calls
    research_complete_called = any(
        tool_call["name"] == "ResearchComplete"
        for tool_call in most_recent_message.tool_calls
    )

    # Premature-completion guard (mirrors the supervisor_tools guard): the CLI/subscription
    # tool-selection envelope can pick the no-argument ResearchComplete before any search has
    # run, ending this researcher unit with zero sources/notes (the empty-dossier failure). If
    # ResearchComplete is selected before a single search has returned -- and iterations remain
    # -- withhold it: answer the ResearchComplete tool call(s) with a corrective nudge (keeping
    # any real tool outputs from this turn) and loop back so the researcher actually searches.
    # The max_react_tool_calls cap above still bounds the loop if the model refuses.
    search_tool_names = {
        name for name, tool in tools_by_name.items()
        if getattr(tool, "metadata", None) and (tool.metadata or {}).get("type") == "search"
    }
    conducted_search = (
        any(isinstance(m, ToolMessage) and getattr(m, "name", "") in search_tool_names
            for m in researcher_messages)
        or any(getattr(t, "name", "") in search_tool_names for t in tool_outputs)
        or has_native_search
    )
    if research_complete_called and not conducted_search and not exceeded_iterations:
        rc_ids = {
            tool_call["id"] for tool_call in most_recent_message.tool_calls
            if tool_call["name"] == "ResearchComplete"
        }
        kept = [t for t in tool_outputs if getattr(t, "tool_call_id", None) not in rc_ids]
        nudge = [
            ToolMessage(
                content=(
                    "No research has been conducted yet. Before calling ResearchComplete you "
                    "MUST call the web search tool at least once to gather sourced information. "
                    "Do not rely on facts stated in the request -- verify them with searches. "
                    "Run the necessary searches now."
                ),
                name="ResearchComplete",
                tool_call_id=rc_id,
            )
            for rc_id in rc_ids
        ]
        return Command(
            goto="researcher",
            update={"researcher_messages": kept + nudge},
        )

    if exceeded_iterations or research_complete_called:
        # End research and proceed to compression
        return Command(
            goto="compress_research",
            update={"researcher_messages": tool_outputs}
        )

    # Continue research loop with tool results
    return Command(
        goto="researcher",
        update={"researcher_messages": tool_outputs}
    )

async def compress_research(state: ResearcherState, config: RunnableConfig):
    """Compress and synthesize research findings into a concise, structured summary.

    This function takes all the research findings, tool outputs, and AI messages from
    a researcher's work and distills them into a clean, comprehensive summary while
    preserving all important information and findings.

    Args:
        state: Current researcher state with accumulated research messages
        config: Runtime configuration with compression model settings

    Returns:
        Dictionary containing compressed research summary and raw notes
    """
    # Step 1: Configure the compression model
    configurable = Configuration.from_runnable_config(config)
    synthesizer_model = configurable_model.with_config({
        "model": configurable.compression_model,
        "model_chain": configurable.model_chain("compression"),
        "stage": "compression",
        "max_tokens": configurable.compression_model_max_tokens,
        "api_key": get_api_key_for_model(configurable.compression_model, config),
        "tags": ["langsmith:nostream"]
    })

    # Step 2: Prepare messages for compression
    researcher_messages = state.get("researcher_messages", [])

    # Add instruction to switch from research mode to compression mode
    researcher_messages.append(HumanMessage(content=compress_research_simple_human_message))

    # Step 3: Attempt compression with retry logic for token limit issues
    synthesis_attempts = 0
    max_attempts = 3

    while synthesis_attempts < max_attempts:
        try:
            # Create system prompt focused on compression task
            compression_prompt = compress_research_system_prompt.format(date=get_today_str())
            messages = [SystemMessage(content=compression_prompt)] + researcher_messages

            # Execute compression
            response = await synthesizer_model.ainvoke(messages)

            # Extract raw notes from all tool and AI messages
            raw_notes_content = "\n".join([
                str(message.content)
                for message in filter_messages(researcher_messages, include_types=["tool", "ai"])
            ])

            # Return successful compression result
            return {
                "compressed_research": str(response.content),
                "raw_notes": [raw_notes_content]
            }

        except Exception as e:
            synthesis_attempts += 1

            # Handle token limit exceeded by removing older messages
            if is_token_limit_exceeded(e, configurable.researcher_model):
                logger.warning("compress_research attempt %d hit token limit; trimming history: %s",
                               synthesis_attempts, e)
                researcher_messages = remove_up_to_last_ai_message(researcher_messages)
                continue

            # For other errors, log (don't silently discard) and retry.
            logger.warning("compress_research attempt %d failed: %s",
                           synthesis_attempts, e, exc_info=True)
            continue

    # Step 4: Return error result if all attempts failed
    logger.error("compress_research exhausted %d attempts; returning failure sentinel", max_attempts)
    raw_notes_content = "\n".join([
        str(message.content)
        for message in filter_messages(researcher_messages, include_types=["tool", "ai"])
    ])

    return {
        "compressed_research": COMPRESSION_FAILED_SENTINEL,
        "raw_notes": [raw_notes_content]
    }

# Researcher Subgraph Construction
# Creates individual researcher workflow for conducting focused research on specific topics
researcher_builder = StateGraph(
    ResearcherState,
    output=ResearcherOutputState,
    config_schema=Configuration
)

# Add researcher nodes for research execution and compression
researcher_builder.add_node("researcher", researcher)                 # Main researcher logic
researcher_builder.add_node("researcher_tools", researcher_tools)     # Tool execution handler
researcher_builder.add_node("compress_research", compress_research)   # Research compression

# Define researcher workflow edges
researcher_builder.add_edge(START, "researcher")           # Entry point to researcher
researcher_builder.add_edge("compress_research", END)      # Exit point after compression

# Compile researcher subgraph for parallel execution by supervisor
researcher_subgraph = researcher_builder.compile()
