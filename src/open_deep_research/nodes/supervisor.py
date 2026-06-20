"""Supervisor node, tool execution, and the supervisor subgraph."""

import asyncio
import logging
import os
from typing import Literal

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from open_deep_research.claude_agent_chat import configurable_claude_model
from open_deep_research.configuration import Configuration
from open_deep_research.nodes.common import (
    ALL_RESEARCH_FAILED_SENTINEL,
    COMPRESSION_FAILED_SENTINEL,
)
from open_deep_research.nodes.researcher import researcher_subgraph
from open_deep_research.state import (
    ConductResearch,
    ResearchComplete,
    SupervisorState,
)
from open_deep_research.utils import (
    get_api_key_for_model,
    get_notes_from_tool_calls,
    is_token_limit_exceeded,
    think_tool,
)

logger = logging.getLogger(__name__)

# Initialize a configurable model that we will use throughout the supervisor.
configurable_model = configurable_claude_model(
    default_config={"model": "gemini:gemini-2.5-flash"}
)


def _lead_researcher_tools(conducted_research: bool) -> list:
    """Tools offered to the supervisor, conditioned on whether research has run yet.

    The CLI/subscription backends select tools by *name* via a JSON envelope that does
    not enforce per-tool argument schemas, so the no-argument ResearchComplete is always
    a valid selection. If it is offered before any research has run, the supervisor picks
    it every turn and never dispatches research; the premature-completion guard in
    ``supervisor_tools`` then merely loops until the iteration cap, ending with empty
    notes. Withholding ResearchComplete (and think_tool) until a ConductResearch result
    exists forces a real dispatch first. Once research has returned, the full toolset is
    available so the supervisor can reflect and legitimately complete.
    """
    if conducted_research:
        return [ConductResearch, ResearchComplete, think_tool]
    return [ConductResearch]


async def supervisor(state: SupervisorState, config: RunnableConfig) -> Command[Literal["supervisor_tools"]]:
    """Lead research supervisor that plans research strategy and delegates to researchers.

    The supervisor analyzes the research brief and decides how to break down the research
    into manageable tasks. It can use think_tool for strategic planning, ConductResearch
    to delegate tasks to sub-researchers, or ResearchComplete when satisfied with findings.

    Args:
        state: Current supervisor state with messages and research context
        config: Runtime configuration with model settings

    Returns:
        Command to proceed to supervisor_tools for tool execution
    """
    # Step 1: Configure the supervisor model with available tools
    configurable = Configuration.from_runnable_config(config)
    supervisor_model_config = {
        "model": configurable.supervisor_model,
        "model_chain": configurable.model_chain("supervisor"),
        "stage": "supervisor",
        "max_tokens": configurable.researcher_model_max_tokens,
        "api_key": get_api_key_for_model(configurable.supervisor_model, config),
        "tags": ["langsmith:nostream"]
    }

    # Step 2: Choose tools based on progress. Until at least one ConductResearch result
    # has returned, withhold ResearchComplete so the supervisor cannot prematurely complete
    # via the no-argument envelope selection -- it must dispatch real research first.
    supervisor_messages = state.get("supervisor_messages", [])
    conducted_research = any(
        isinstance(message, ToolMessage) and getattr(message, "name", "") == "ConductResearch"
        for message in supervisor_messages
    )
    lead_researcher_tools = _lead_researcher_tools(conducted_research)

    # Configure model with tools, retry logic, and model settings
    supervisor_model = (
        configurable_model
        .bind_tools(lead_researcher_tools)
        .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
        .with_config(supervisor_model_config)
    )

    # Step 3: Generate supervisor response based on current context
    response = await supervisor_model.ainvoke(supervisor_messages)

    # Step 4: Update state and proceed to tool execution
    return Command(
        goto="supervisor_tools",
        update={
            "supervisor_messages": [response],
            "research_iterations": state.get("research_iterations", 0) + 1
        }
    )

async def supervisor_tools(state: SupervisorState, config: RunnableConfig) -> Command[Literal["supervisor", "__end__"]]:
    """Execute tools called by the supervisor, including research delegation and strategic thinking.

    This function handles three types of supervisor tool calls:
    1. think_tool - Strategic reflection that continues the conversation
    2. ConductResearch - Delegates research tasks to sub-researchers
    3. ResearchComplete - Signals completion of research phase

    Args:
        state: Current supervisor state with messages and iteration count
        config: Runtime configuration with research limits and model settings

    Returns:
        Command to either continue supervision loop or end research phase
    """
    # Step 1: Extract current state and check exit conditions
    configurable = Configuration.from_runnable_config(config)
    supervisor_messages = state.get("supervisor_messages", [])
    research_iterations = state.get("research_iterations", 0)
    most_recent_message = supervisor_messages[-1]

    # Define exit criteria for research phase
    exceeded_allowed_iterations = research_iterations > configurable.max_researcher_iterations
    no_tool_calls = not most_recent_message.tool_calls
    research_complete_tool_call = any(
        tool_call["name"] == "ResearchComplete"
        for tool_call in most_recent_message.tool_calls
    )

    # Guard against premature completion: the CLI/subscription backends select tools via a
    # JSON envelope that does not enforce per-tool argument schemas, so the supervisor can
    # satisfy it with the no-argument ResearchComplete and finish before any research runs --
    # leaving notes/raw_notes empty. If ResearchComplete is called before a single
    # ConductResearch has returned, don't end: answer the tool call with a corrective nudge and
    # loop back so the supervisor actually dispatches research. The iteration cap above still
    # bounds the loop if the model refuses to comply.
    conducted_research = any(
        isinstance(message, ToolMessage) and getattr(message, "name", "") == "ConductResearch"
        for message in supervisor_messages
    )
    research_complete_calls = [
        tool_call for tool_call in most_recent_message.tool_calls
        if tool_call["name"] == "ResearchComplete"
    ]
    if research_complete_calls and not conducted_research and not exceeded_allowed_iterations:
        corrective_messages = [
            ToolMessage(
                content=(
                    "No research has been conducted yet. Before calling ResearchComplete you "
                    "must call ConductResearch with one or more specific, standalone "
                    "research_topic instructions. Dispatch the necessary research now."
                ),
                name="ResearchComplete",
                tool_call_id=tool_call["id"],
            )
            for tool_call in research_complete_calls
        ]
        return Command(
            goto="supervisor",
            update={"supervisor_messages": corrective_messages},
        )

    # Guard against a blank turn (model returned text / no tool call) before any research ran.
    # The CLI backends raise on a bad envelope, but an API model (e.g. NVIDIA) can return a
    # text AIMessage with empty tool_calls -> the old no_tool_calls exit ended research empty
    # (the Brazil failure). Nudge it to dispatch ConductResearch and loop, bounded by the cap.
    if no_tool_calls and not conducted_research and not exceeded_allowed_iterations:
        return Command(
            goto="supervisor",
            update={"supervisor_messages": [HumanMessage(content=(
                "You did not call any tool. You MUST call ConductResearch with one or more "
                "specific, standalone research_topic instructions before finishing. "
                "Dispatch the necessary research now."))]},
        )

    # Exit if any termination condition is met
    if exceeded_allowed_iterations or no_tool_calls or research_complete_tool_call:
        return Command(
            goto=END,
            update={
                "notes": get_notes_from_tool_calls(supervisor_messages),
                "research_brief": state.get("research_brief", "")
            }
        )

    # Step 2: Process all tool calls together (both think_tool and ConductResearch)
    all_tool_messages = []
    update_payload = {"supervisor_messages": []}

    # Handle think_tool calls (strategic reflection)
    think_tool_calls = [
        tool_call for tool_call in most_recent_message.tool_calls
        if tool_call["name"] == "think_tool"
    ]

    for tool_call in think_tool_calls:
        # Some CLI backends (gemini/codex) coerce tool args and may omit 'reflection';
        # tolerate that instead of KeyError-ing the whole research turn.
        reflection_content = (tool_call.get("args") or {}).get("reflection", "")
        all_tool_messages.append(ToolMessage(
            content=f"Reflection recorded: {reflection_content}",
            name="think_tool",
            tool_call_id=tool_call["id"]
        ))

    # Handle ConductResearch calls (research delegation)
    conduct_research_calls = [
        tool_call for tool_call in most_recent_message.tool_calls
        if tool_call["name"] == "ConductResearch"
    ]

    # The tool-selection envelope doesn't enforce per-tool argument schemas, so a
    # ConductResearch call can arrive with an empty or missing research_topic (especially
    # when it is the only tool offered pre-research). Dispatching that would KeyError on
    # args["research_topic"] and research nothing -- answer each with a corrective nudge
    # and drop it so only calls with a real topic are dispatched. If all of them were empty
    # and there are no think_tool calls, the nudges alone loop the supervisor back to retry.
    empty_research_calls = [
        tool_call for tool_call in conduct_research_calls
        if not str((tool_call.get("args") or {}).get("research_topic", "")).strip()
    ]
    for tool_call in empty_research_calls:
        all_tool_messages.append(ToolMessage(
            content=(
                "ConductResearch requires a non-empty 'research_topic': a specific, "
                "standalone instruction describing exactly what to research. Provide one "
                "and dispatch ConductResearch again."
            ),
            name="ConductResearch",
            tool_call_id=tool_call["id"],
        ))
    conduct_research_calls = [
        tool_call for tool_call in conduct_research_calls
        if tool_call not in empty_research_calls
    ]

    if conduct_research_calls:
        try:
            # Limit concurrent research units to prevent resource exhaustion
            allowed_conduct_research_calls = conduct_research_calls[:configurable.max_concurrent_research_units]
            overflow_conduct_research_calls = conduct_research_calls[configurable.max_concurrent_research_units:]

            # Execute research tasks in parallel, each under an overall wall-clock budget.
            # Per-call timeouts bound individual stalls, but a researcher runs many
            # turns x tool calls x retries, so without an aggregate cap its worst case is
            # effectively unbounded. On timeout the unit raises TimeoutError, handled as a
            # per-unit failure below (the budget comfortably exceeds a healthy run).
            researcher_budget_s = float(os.getenv("RESEARCHER_BUDGET_S", "1800"))
            research_tasks = [
                asyncio.wait_for(
                    researcher_subgraph.ainvoke({
                        "researcher_messages": [
                            HumanMessage(content=tool_call["args"]["research_topic"])
                        ],
                        "research_topic": tool_call["args"]["research_topic"]
                    }, config),
                    timeout=researcher_budget_s,
                )
                for tool_call in allowed_conduct_research_calls
            ]

            # return_exceptions=True so one researcher failing (or timing out) does NOT
            # cancel the others and discard their completed work -- each failure is
            # isolated into a per-unit error ToolMessage below.
            tool_results = await asyncio.gather(*research_tasks, return_exceptions=True)

            # Create tool messages with research results. A failed unit becomes an error
            # ToolMessage for THAT topic (the supervisor sees it and can react) rather than
            # aborting the whole batch.
            for observation, tool_call in zip(tool_results, allowed_conduct_research_calls):
                if isinstance(observation, BaseException):
                    logger.error(
                        "Research unit failed for topic %r: %s",
                        tool_call["args"].get("research_topic"), observation,
                        exc_info=observation,
                    )
                    all_tool_messages.append(ToolMessage(
                        content=f"Error: this research unit failed and produced no findings: {observation}",
                        name=tool_call["name"],
                        tool_call_id=tool_call["id"]
                    ))
                    continue
                all_tool_messages.append(ToolMessage(
                    content=observation.get("compressed_research", COMPRESSION_FAILED_SENTINEL),
                    name=tool_call["name"],
                    tool_call_id=tool_call["id"]
                ))

            # Handle overflow research calls with error messages
            for overflow_call in overflow_conduct_research_calls:
                all_tool_messages.append(ToolMessage(
                    content=f"Error: Did not run this research as you have already exceeded the maximum number of concurrent research units. Please try again with {configurable.max_concurrent_research_units} or fewer research units.",
                    name="ConductResearch",
                    tool_call_id=overflow_call["id"]
                ))

            # Aggregate raw notes from the SUCCESSFUL research results only.
            raw_notes_concat = "\n".join([
                "\n".join(observation.get("raw_notes", []))
                for observation in tool_results
                if not isinstance(observation, BaseException)
            ])

            allowed_n = len(allowed_conduct_research_calls)
            all_failed = allowed_n > 0 and all(
                isinstance(o, BaseException) for o in tool_results
            )
            if all_failed and not raw_notes_concat:
                from open_deep_research.failover import get_tracker
                fos = get_tracker((config.get("configurable") or {}).get("thread_id")).failovers
                logger.error("All %d research units failed and produced no notes; "
                             "failovers=%s", allowed_n, [f.as_dict() for f in fos])
                update_payload["raw_notes"] = [ALL_RESEARCH_FAILED_SENTINEL]

            if raw_notes_concat:
                update_payload["raw_notes"] = [raw_notes_concat]

        except Exception as e:
            # Per-unit researcher failures are handled above (return_exceptions=True), so
            # anything reaching here is either a genuine token-limit (end gracefully) or an
            # unexpected bug in the dispatch/aggregation code (surface it -- do not pretend
            # it was a clean completion, which silently truncates research).
            if is_token_limit_exceeded(e, configurable.supervisor_model):
                logger.warning("Supervisor research hit a token limit; ending research phase: %s", e)
                return Command(
                    goto=END,
                    update={
                        "notes": get_notes_from_tool_calls(supervisor_messages),
                        "research_brief": state.get("research_brief", "")
                    }
                )
            logger.error("Research dispatch failed unexpectedly: %s", e, exc_info=True)
            raise

    # Step 3: Return command with all tool results
    update_payload["supervisor_messages"] = all_tool_messages
    return Command(
        goto="supervisor",
        update=update_payload
    )

# Supervisor Subgraph Construction
# Creates the supervisor workflow that manages research delegation and coordination
supervisor_builder = StateGraph(SupervisorState, config_schema=Configuration)

# Add supervisor nodes for research management
supervisor_builder.add_node("supervisor", supervisor)           # Main supervisor logic
supervisor_builder.add_node("supervisor_tools", supervisor_tools)  # Tool execution handler

# Define supervisor workflow edges
supervisor_builder.add_edge(START, "supervisor")  # Entry point to supervisor

# Compile supervisor subgraph for use in main workflow
supervisor_subgraph = supervisor_builder.compile()
