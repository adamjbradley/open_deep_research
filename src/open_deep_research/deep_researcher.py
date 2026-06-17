"""Main LangGraph implementation for the Deep Research agent."""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    filter_messages,
    get_buffer_string,
)
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import BaseModel, Field

from open_deep_research.claude_agent_chat import configurable_claude_model
from open_deep_research.configuration import (
    Configuration,
)
from open_deep_research.prompts import (
    answer_from_dossier_prompt,
    clarify_with_user_instructions,
    compress_research_simple_human_message,
    compress_research_system_prompt,
    facts_answer_polish_prompt,
    final_report_generation_prompt,
    knowledge_assessment_prompt,
    lead_researcher_prompt,
    merge_reports_prompt,
    research_system_prompt,
    subject_resolution_prompt,
    target_properties_prompt,
    transform_messages_into_research_topic_prompt,
)
from open_deep_research.state import (
    AgentInputState,
    AgentState,
    ClarifyWithUser,
    ConductResearch,
    ResearchComplete,
    ResearcherOutputState,
    ResearcherState,
    KnowledgeAssessment,
    ResearchQuestion,
    SubjectResolution,
    SupervisorState,
    TargetProperties,
)
from open_deep_research.storage import (
    extract_sources,
    get_db_path,
    get_subject_by_slug,
    get_subject_names,
    log_research_run,
    preallocate_run as preallocate_run_storage,
    reap_stale_running,
    save_run_and_upsert_subject,
    slugify,
)
from open_deep_research.utils import (
    anthropic_websearch_called,
    get_all_tools,
    get_api_key_for_model,
    get_model_token_limit,
    get_notes_from_tool_calls,
    get_today_str,
    is_token_limit_exceeded,
    openai_websearch_called,
    remove_up_to_last_ai_message,
    think_tool,
)

logger = logging.getLogger(__name__)

# Failure sentinels emitted by compress_research / final_report_generation when their
# model calls exhaust retries. They are real strings that flow into notes / the report,
# so persistence must detect them (see _report_is_failed) and avoid saving them as a
# "completed" run or merging them into the subject dossier (which would poison the KB).
COMPRESSION_FAILED_SENTINEL = "Error synthesizing research report: Maximum retries exceeded"
REPORT_FAILED_PREFIX = "Error generating final report:"


def _report_is_failed(report: Optional[str]) -> bool:
    """Whether a final report is empty or a generation-failure sentinel (not real content)."""
    if not report or not report.strip():
        return True
    stripped = report.strip()
    return stripped.startswith(REPORT_FAILED_PREFIX) or stripped == COMPRESSION_FAILED_SENTINEL


def recommended_recursion_limit(
    max_researcher_iterations: int, max_concurrent_research_units: int = 1
) -> int:
    """A LangGraph ``recursion_limit`` (super-step budget) that covers a full run.

    The supervisor loop is ~2 super-steps per turn and runs up to
    ``max_researcher_iterations + 1`` turns; add the linear parent chain
    (clarify -> brief -> preallocate -> assess -> supervisor -> report -> extract ->
    persist) plus headroom. LangGraph's default of 25 can be exceeded by a legitimate
    high-iteration run, crashing mid-research with ``GraphRecursionError``. Callers that
    own the invocation should pass this via ``config={"recursion_limit": ...}``.
    (The hosted Studio/dev server sets its own limit; this only governs our own invokes.)
    """
    return 4 * max(1, max_researcher_iterations) + 25


from open_deep_research.factbase import fetch as _fb_fetch


async def _fact_fetch_text(url, **kw):
    return await _fb_fetch.fetch_text(url, **kw)


# Initialize a configurable model that we will use throughout the agent.
# Backed by CLI agents (Gemini, Claude/code, or Codex). Default = gemini:gemini-2.5-flash:
# the standard Gemini CLI is reliable for structured output; Codex's exec sandbox runs
# repo commands (e.g. pytest) so it stays opt-in per role until that's restricted.
configurable_model = configurable_claude_model(
    default_config={"model": "gemini:gemini-2.5-flash"}
)

async def clarify_with_user(state: AgentState, config: RunnableConfig) -> Command[Literal["write_research_brief", "__end__"]]:
    """Analyze user messages and ask clarifying questions if the research scope is unclear.
    
    This function determines whether the user's request needs clarification before proceeding
    with research. If clarification is disabled or not needed, it proceeds directly to research.
    
    Args:
        state: Current agent state containing user messages
        config: Runtime configuration with model settings and preferences
        
    Returns:
        Command to either end with a clarifying question or proceed to research brief
    """
    # Step 1: Check if clarification is enabled in configuration
    configurable = Configuration.from_runnable_config(config)
    if not configurable.allow_clarification:
        # Skip clarification step and proceed directly to research
        return Command(goto="write_research_brief")
    
    # Step 2: Prepare the model for structured clarification analysis
    messages = state["messages"]
    model_config = {
        "model": configurable.supervisor_model,
        "max_tokens": configurable.researcher_model_max_tokens,
        "api_key": get_api_key_for_model(configurable.supervisor_model, config),
        "tags": ["langsmith:nostream"]
    }
    
    # Configure model with structured output and retry logic
    clarification_model = (
        configurable_model
        .with_structured_output(ClarifyWithUser)
        .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
        .with_config(model_config)
    )
    
    # Step 3: Analyze whether clarification is needed
    prompt_content = clarify_with_user_instructions.format(
        messages=get_buffer_string(messages), 
        date=get_today_str()
    )
    response = await clarification_model.ainvoke([HumanMessage(content=prompt_content)])
    
    # Step 4: Route based on clarification analysis
    if response.need_clarification:
        # End with clarifying question for user
        return Command(
            goto=END, 
            update={"messages": [AIMessage(content=response.question)]}
        )
    else:
        # Proceed to research with verification message
        return Command(
            goto="write_research_brief", 
            update={"messages": [AIMessage(content=response.verification)]}
        )


async def assess_knowledge(state: AgentState, config: RunnableConfig) -> Command[Literal["answer_from_dossier", "write_research_brief", "clarify_with_user"]]:
    """Entry node: match the question to a stored subject and decide how to handle it.

    - The dossier already answers the question  -> answer straight from the cache.
    - Partially covered (or a refresh is asked) -> research the gap and merge.
    - A brand-new subject                       -> clarify scope (optional) then research.

    Args:
        state: Current agent state containing the user's messages
        config: Runtime configuration with knowledge-base settings

    Returns:
        Command routing to answer-from-cache, research, or clarification.
    """
    configurable = Configuration.from_runnable_config(config)
    question = get_buffer_string(state.get("messages", []))

    # Knowledge base disabled: preserve the original clarify -> research flow.
    if not configurable.use_knowledge_base:
        return Command(goto="clarify_with_user")

    db_path = get_db_path(config)

    # Step 1: Ensure the subject matches (reuse an existing subject when applicable).
    try:
        existing_names = await get_subject_names(db_path)
        subject = await _resolve_subject(question, question, existing_names, configurable, config)
    except Exception as e:
        logger.warning("Subject match failed in assess_knowledge: %s", e)
        return Command(goto="clarify_with_user")

    existing = await get_subject_by_slug(db_path, slugify(subject))
    dossier = (existing or {}).get("current_report") if existing else None

    # Step 2: New subject -> clarify scope (if enabled) then research.
    if not dossier:
        target = "clarify_with_user" if configurable.allow_clarification else "write_research_brief"
        return Command(goto=target, update={"subject": subject})

    # Step 3: We have prior knowledge -> does it already answer the question?
    is_answerable = False
    missing_information = ""
    try:
        assessment_model = (
            configurable_model
            .with_structured_output(KnowledgeAssessment)
            .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
            .with_config({
                "model": configurable.supervisor_model,
                "max_tokens": configurable.researcher_model_max_tokens,
                "api_key": get_api_key_for_model(configurable.supervisor_model, config),
                "tags": ["langsmith:nostream"],
            })
        )
        assessment = await assessment_model.ainvoke([HumanMessage(content=knowledge_assessment_prompt.format(
            subject=subject, date=get_today_str(), research_brief=question, dossier=dossier,
        ))])
        is_answerable = bool(assessment.is_answerable)
        missing_information = assessment.missing_information or ""
    except Exception as e:
        logger.warning("Knowledge assessment failed, treating as a gap: %s", e)

    if is_answerable:
        # Fully covered: answer directly from the stored dossier.
        return Command(goto="answer_from_dossier", update={"subject": subject})
    # Partial / refresh: research the gap (subject already known, skip clarification).
    return Command(
        goto="write_research_brief",
        update={"subject": subject, "missing_information": missing_information},
    )


async def answer_from_dossier(state: AgentState, config: RunnableConfig) -> dict:
    """Answer the question directly from the subject's stored dossier (no research)."""
    configurable = Configuration.from_runnable_config(config)
    subject = state.get("subject")
    question = get_buffer_string(state.get("messages", []))
    existing = await get_subject_by_slug(get_db_path(config), slugify(subject)) if subject else None
    dossier = (existing or {}).get("current_report") if existing else ""
    updated_at = (existing or {}).get("updated_at") or get_today_str()

    answer_model = configurable_model.with_config({
        "model": configurable.final_report_model,
        "max_tokens": configurable.final_report_model_max_tokens,
        "api_key": get_api_key_for_model(configurable.final_report_model, config),
        "tags": ["langsmith:nostream"],
    })
    response = await answer_model.ainvoke([HumanMessage(content=answer_from_dossier_prompt.format(
        subject=subject, question=question, updated_at=updated_at, dossier=dossier,
    ))])
    answer = str(response.content)
    # Static edge to persist_research (logs the Q&A run; dossier is unchanged).
    return {
        "final_report": answer,
        "messages": [AIMessage(content=answer)],
        "answered_from_cache": True,
        "subject": subject,
    }


async def write_research_brief(state: AgentState, config: RunnableConfig) -> dict:
    """Build the research brief and initialize the supervisor.

    If we already have a dossier for the resolved subject, the brief is gap-scoped
    (research what's missing, verify the rest, include the dossier as context).
    Otherwise it is generated from the user's messages as a fresh research question.

    Args:
        state: Current agent state containing user messages (and resolved subject)
        config: Runtime configuration with model settings

    Returns:
        State update with the research brief and initialized supervisor messages
    """
    configurable = Configuration.from_runnable_config(config)
    question = get_buffer_string(state.get("messages", []))
    subject = state.get("subject")
    missing_information = state.get("missing_information") or ""

    # Facts-first answers/sufficiency resolve the fact-base instance from `subject`, but on
    # the research path subject is otherwise only resolved at persist time (and not at all
    # when the KB is off). Resolve it here so the facts nodes have an instance to query.
    if configurable.facts_first_mode and not subject:
        try:
            existing_names = await get_subject_names(get_db_path(config))
            subject = await _resolve_subject(question, question, existing_names, configurable, config)
        except Exception as e:
            logger.warning("facts-first subject resolution failed: %s", e)

    # Load the dossier (if any) for the resolved subject to scope the research.
    dossier = None
    if subject:
        existing = await get_subject_by_slug(get_db_path(config), slugify(subject))
        dossier = (existing or {}).get("current_report") if existing else None

    if dossier:
        # Gap research: focus on what's missing, verify the rest, include the dossier.
        research_brief = (
            f"Research the subject \"{subject}\" to fully answer this question:\n{question}\n\n"
            f"Focus in particular on the information that is currently missing: "
            f"{missing_information or '(complete or refresh any out-of-date facts)'}\n\n"
            f"Verify the existing facts below against current sources and extend them; "
            f"do not merely repeat them:\n{dossier}"
        )
    else:
        # New subject: generate a focused research brief from the user's messages.
        supervisor_model = (
            configurable_model
            .with_structured_output(ResearchQuestion)
            .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
            .with_config({
                "model": configurable.supervisor_model,
                "max_tokens": configurable.researcher_model_max_tokens,
                "api_key": get_api_key_for_model(configurable.supervisor_model, config),
                "tags": ["langsmith:nostream"],
            })
        )
        response = await supervisor_model.ainvoke([HumanMessage(content=transform_messages_into_research_topic_prompt.format(
            messages=question, date=get_today_str()
        ))])
        research_brief = response.research_brief

    # Facts-first: resolve which fact properties the question needs and steer research at them.
    target_properties = state.get("target_properties")
    if configurable.facts_first_mode and not target_properties:
        from open_deep_research.factbase import profile as _fbprofile
        target_properties = await resolve_target_properties(
            question, _fbprofile.load(configurable.profile_name), configurable, config
        )
    if configurable.facts_first_mode and target_properties:
        research_brief = (
            f"{research_brief}\n\nGather the specific facts needed to answer this. Focus on these "
            f"properties: {', '.join(target_properties)}. For each, find the value with a citation."
        )

    supervisor_system_prompt = lead_researcher_prompt.format(
        date=get_today_str(),
        max_concurrent_research_units=configurable.max_concurrent_research_units,
        max_researcher_iterations=configurable.max_researcher_iterations
    )
    # Routed to research_supervisor (see graph wiring).
    update = {
        "research_brief": research_brief,
        "subject": subject,
        "supervisor_messages": {
            "type": "override",
            "value": [
                SystemMessage(content=supervisor_system_prompt),
                HumanMessage(content=research_brief)
            ]
        }
    }
    if target_properties:
        update["target_properties"] = target_properties
    return update


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

async def final_report_generation(state: AgentState, config: RunnableConfig):
    """Generate the final comprehensive research report with retry logic for token limits.
    
    This function takes all collected research findings and synthesizes them into a 
    well-structured, comprehensive final report using the configured report generation model.
    
    Args:
        state: Agent state containing research findings and context
        config: Runtime configuration with model settings and API keys
        
    Returns:
        Dictionary containing the final report and cleared state
    """
    # Step 1: Extract research findings and prepare state cleanup
    notes = state.get("notes", [])
    cleared_state = {"notes": {"type": "override", "value": []}}
    findings = "\n".join(notes)
    
    # Step 2: Configure the final report generation model
    configurable = Configuration.from_runnable_config(config)
    writer_model_config = {
        "model": configurable.final_report_model,
        "max_tokens": configurable.final_report_model_max_tokens,
        "api_key": get_api_key_for_model(configurable.final_report_model, config),
        "tags": ["langsmith:nostream"]
    }
    
    # Step 3: Attempt report generation with token limit retry logic
    max_retries = 3
    current_retry = 0
    findings_token_limit = None
    
    while current_retry <= max_retries:
        try:
            # Create comprehensive prompt with all research context
            final_report_prompt = final_report_generation_prompt.format(
                research_brief=state.get("research_brief", ""),
                messages=get_buffer_string(state.get("messages", [])),
                findings=findings,
                date=get_today_str()
            )
            
            # Generate the final report
            final_report = await configurable_model.with_config(writer_model_config).ainvoke([
                HumanMessage(content=final_report_prompt)
            ])
            
            # Return successful report generation
            return {
                "final_report": final_report.content, 
                "messages": [final_report],
                **cleared_state
            }
            
        except Exception as e:
            # Handle token limit exceeded errors with progressive truncation
            if is_token_limit_exceeded(e, configurable.final_report_model):
                current_retry += 1
                
                if current_retry == 1:
                    # First retry: determine initial truncation limit
                    model_token_limit = get_model_token_limit(configurable.final_report_model)
                    if not model_token_limit:
                        return {
                            "final_report": f"Error generating final report: Token limit exceeded, however, we could not determine the model's maximum context length. Please update the model map in deep_researcher/utils.py with this information. {e}",
                            "messages": [AIMessage(content="Report generation failed due to token limits")],
                            **cleared_state
                        }
                    # Use 4x token limit as character approximation for truncation
                    findings_token_limit = model_token_limit * 4
                else:
                    # Subsequent retries: reduce by 10% each time
                    findings_token_limit = int(findings_token_limit * 0.9)
                
                # Truncate findings and retry
                findings = findings[:findings_token_limit]
                continue
            else:
                # Non-token-limit error: return error immediately
                logger.error("Final report generation failed: %s", e, exc_info=True)
                return {
                    "final_report": f"{REPORT_FAILED_PREFIX} {e}",
                    "messages": [AIMessage(content="Report generation failed due to an error")],
                    **cleared_state
                }

    # Step 4: Return failure result if all retries exhausted
    logger.error("Final report generation exhausted retries (token limits)")
    return {
        "final_report": f"{REPORT_FAILED_PREFIX} Maximum retries exceeded",
        "messages": [AIMessage(content="Report generation failed after maximum retries")],
        **cleared_state
    }

async def _resolve_subject(topic, research_brief, existing_subjects, configurable, config):
    """Use a cheap model to map this query to a canonical subject name.

    Reuses an existing subject's name verbatim when the query concerns it (even a
    different aspect), so accumulation groups related runs together.
    """
    model = (
        configurable_model
        .with_structured_output(SubjectResolution)
        .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
        .with_config({
            "model": configurable.summarization_model,
            "max_tokens": configurable.summarization_model_max_tokens,
            "api_key": get_api_key_for_model(configurable.summarization_model, config),
            "tags": ["langsmith:nostream"],
        })
    )
    existing = "\n".join(f"- {s}" for s in existing_subjects) or "(none yet)"
    prompt = subject_resolution_prompt.format(
        topic=topic, research_brief=research_brief or topic, existing_subjects=existing
    )
    response = await model.ainvoke([HumanMessage(content=prompt)])
    return response.subject.strip()


async def resolve_target_properties(question, prof, configurable, config) -> list[str]:
    """Map a question to the subset of profile properties needed to answer it (facts-first).

    A cheap structured call; names are validated against the profile and unknowns dropped.
    Falls back to ALL property names on any failure or empty/all-invalid result, so the
    fact path is never starved.
    """
    all_names = [pd.name for pd in prof.properties]
    try:
        model = (
            configurable_model
            .with_structured_output(TargetProperties)
            .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
            .with_config({
                "model": configurable.summarization_model,
                "max_tokens": configurable.summarization_model_max_tokens,
                "api_key": get_api_key_for_model(configurable.summarization_model, config),
                "tags": ["langsmith:nostream"],
            })
        )
        listing = "\n".join(f"- {pd.name} ({pd.value_kind})" for pd in prof.properties)
        prompt = target_properties_prompt.format(question=question, properties=listing)
        response = await model.ainvoke([HumanMessage(content=prompt)])
        valid = [n for n in (response.property_names or []) if n in all_names]
        return valid or all_names
    except Exception as e:
        logger.warning("resolve_target_properties failed; using all properties: %s", e)
        return all_names


async def _merge_dossier(subject, existing_report, new_report, configurable, config):
    """Merge a new report into a subject's existing dossier (preserve + integrate)."""
    model = configurable_model.with_config({
        "model": configurable.final_report_model,
        "max_tokens": configurable.final_report_model_max_tokens,
        "api_key": get_api_key_for_model(configurable.final_report_model, config),
        "tags": ["langsmith:nostream"],
    })
    prompt = merge_reports_prompt.format(
        subject=subject,
        existing_report=existing_report,
        new_report=new_report,
        date=get_today_str(),
    )
    response = await model.ainvoke([HumanMessage(content=prompt)])
    return str(response.content)


async def persist_research(state: AgentState, config: RunnableConfig):
    """Store the completed run and accumulate it into its subject's dossier.

    Resolves the canonical subject, stores this run (full history) in
    ``research_runs``, and merges the new report into the subject's accumulated
    dossier so later questions about a different aspect of the same subject add
    to -- rather than replace -- existing knowledge. Best-effort: a failure is
    logged but never breaks the completed run.

    Args:
        state: Agent state containing the final report and research data
        config: Runtime configuration with persistence settings

    Returns:
        Dict with the stored run id (``report_id``) and ``subject``, or empty.
    """
    configurable = Configuration.from_runnable_config(config)
    if not configurable.persist_results:
        return {}

    # Topic is the first user message; the brief is the model-derived question.
    messages = state.get("messages", [])
    topic = next(
        (str(m.content) for m in messages if isinstance(m, HumanMessage)),
        get_buffer_string(messages[:1]) if messages else "",
    )
    research_brief = state.get("research_brief")
    final_report = state.get("final_report", "")
    raw_notes = state.get("raw_notes", [])
    new_sources = extract_sources(final_report, *raw_notes)

    config_used = configurable.model_dump(mode="json")
    config_used.pop("mcp_config", None)

    run = {
        "thread_id": (config.get("configurable") or {}).get("thread_id"),
        "topic": topic,
        "research_brief": research_brief,
        "final_report": final_report,
        "sources": new_sources,
        "raw_notes": raw_notes,
        "config": config_used,
        "status": "completed",
        "error": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        db_path = get_db_path(config)
        now = run["created_at"]

        # Answered from the cache: log the Q&A run, but leave the dossier unchanged.
        if state.get("answered_from_cache") and state.get("subject"):
            run["status"] = "answered_from_cache"
            run_id = await log_research_run(
                db_path, slugify(state["subject"]), run, run_id=state.get("prealloc_run_id")
            )
            return {"report_id": run_id, "subject": state["subject"]}

        # Failed/empty report: record the run as an error for history, but do NOT merge
        # the error text into the subject dossier -- that would poison future cache answers
        # (assess_knowledge could later serve the error straight from the dossier).
        if _report_is_failed(final_report):
            run["status"] = "error"
            run["error"] = (final_report or "empty report")[:500]
            subject_for_log = state.get("subject") or topic
            run_id = await log_research_run(
                db_path, slugify(subject_for_log), run, run_id=state.get("prealloc_run_id")
            )
            logger.error(
                "Run produced no usable report (%r...); logged as error, dossier left unchanged.",
                (final_report or "")[:120],
            )
            return {"report_id": run_id, "subject": subject_for_log}

        if configurable.accumulate_by_subject and final_report:
            # 1) Use the subject already matched by assess_knowledge, else resolve now.
            subject_name = state.get("subject")
            if not subject_name:
                existing_names = await get_subject_names(db_path)
                try:
                    subject_name = await _resolve_subject(
                        topic, research_brief, existing_names, configurable, config
                    )
                except Exception as e:
                    logger.warning("Subject resolution failed, using topic: %s", e)
                    subject_name = topic
            slug = slugify(subject_name)

            # 2) Merge into the existing dossier (preserve + add) if one exists.
            existing = await get_subject_by_slug(db_path, slug)
            if existing and existing.get("current_report"):
                subject_name = existing["name"] or subject_name  # keep canonical name
                try:
                    merged_report = await _merge_dossier(
                        subject_name, existing["current_report"], final_report,
                        configurable, config,
                    )
                except Exception as e:
                    # Fallback: concatenate so existing content is never lost.
                    logger.warning("Dossier merge failed, appending instead: %s", e)
                    merged_report = (
                        f"{existing['current_report']}\n\n---\n\n"
                        f"## Additional research ({now[:10]})\n\n{final_report}"
                    )
                # Guard: a successful-but-empty/degenerate merge must not clobber a good
                # dossier. If the merged report vanished or shrank by >50%, append instead.
                prior = existing["current_report"].strip()
                if not merged_report.strip() or len(merged_report.strip()) < 0.5 * len(prior):
                    logger.warning(
                        "Merged dossier shrank unexpectedly (%d -> %d chars); appending instead.",
                        len(prior), len(merged_report.strip()),
                    )
                    merged_report = (
                        f"{existing['current_report']}\n\n---\n\n"
                        f"## Additional research ({now[:10]})\n\n{final_report}"
                    )
                sources_union = extract_sources(merged_report) or list(
                    dict.fromkeys([*existing.get("sources", []), *new_sources])
                )
            else:
                merged_report = final_report
                sources_union = new_sources
        else:
            # No accumulation: each run is its own subject snapshot (no LLM calls).
            subject_name = topic
            slug = slugify(topic)
            merged_report = final_report
            sources_union = new_sources

        subject_id, run_id = await save_run_and_upsert_subject(
            db_path,
            subject_name=subject_name,
            slug=slug,
            merged_report=merged_report,
            sources_union=sources_union,
            run=run,
            now=now,
            run_id=state.get("prealloc_run_id"),
        )
        return {"report_id": run_id, "subject": subject_name}
    except Exception as e:
        # Persistence is best-effort: never fail a completed run on a DB error. But for a
        # knowledge-base product a silent save failure breaks the whole value prop, so log
        # at error with a stack and surface a marker the caller/UI can use to warn the user.
        logger.error("Failed to persist research result: %s", e, exc_info=True)
        return {"persist_error": str(e)}


###################
# Fact-base extraction (structured output models + helpers)
###################
class FactRecord(BaseModel):
    """A single extracted country digital-identity fact."""

    property: str
    instance_name: str
    value: str
    unit: Optional[str] = None
    as_of: Optional[str] = None
    qualifiers: dict = Field(default_factory=dict)
    evidence_span: str


class ExtractionResult(BaseModel):
    """List of facts extracted from a single source."""

    facts: list[FactRecord] = Field(default_factory=list)


def _make_fact_model_call(configurable, config, target_properties=None):
    """Build an async model_call(source_text, prof) -> list[dict] for the extractor.

    Mirrors the structured-output invocation pattern used elsewhere in this graph
    (singleton ``configurable_model`` -> ``with_structured_output`` -> ``with_config``
    -> ``ainvoke``). Best-effort: returns [] on any error so extraction never fails
    a completed run. ``target_properties`` (facts-first mode) narrows extraction to the
    properties the question needs; default = all profile properties.
    """
    async def model_call(source_text, prof):
        try:
            from open_deep_research.factbase.prompting import build_extraction_prompt
            prompt = build_extraction_prompt(
                prof, target_properties, source_text,
                compiled=configurable.compile_extraction_prompt,
            )
            if configurable.compile_extraction_prompt and len(prompt) > 12000:
                logger.warning(
                    "Compiled extraction prompt is large (%d chars) for entity_type=%s; "
                    "consider trimming the profile.", len(prompt), prof.entity_type)
            extraction_model = configurable.model_for("extract_facts", "researcher")
            model = (
                configurable_model
                .with_structured_output(ExtractionResult)
                .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
                .with_config({
                    "model": extraction_model,
                    "max_tokens": configurable.researcher_model_max_tokens,
                    "api_key": get_api_key_for_model(configurable.researcher_model, config),
                    "tags": ["langsmith:nostream"],
                })
            )
            result = await model.ainvoke([HumanMessage(content=prompt)])
            return [f.model_dump() for f in (result.facts or [])]
        except Exception as e:
            logger.warning("fact model_call failed (non-fatal): %s", e)
            return []
    return model_call


async def preallocate_run(state: AgentState, config: RunnableConfig) -> dict:
    """Create the research_runs row early so the tool layer/extract_facts share a run id."""
    configurable = Configuration.from_runnable_config(config)
    if not configurable.persist_results:
        return {}
    thread_id = (config.get("configurable") or {}).get("thread_id")
    db_path = get_db_path(config)
    # Reap abandoned runs: any row still 'running' past the staleness window belongs to a
    # crashed/killed prior run (the in-memory graph state is gone, so it will never finalize).
    # Sweep them to status='error' here, at each run's start, so the history stays honest.
    # The window is generous relative to a normal run's wall-clock, so live runs are safe.
    try:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=configurable.run_staleness_minutes)
        ).isoformat()
        reaped = await reap_stale_running(db_path, cutoff)
        if reaped:
            logger.info("Reaped %d stale 'running' research run(s) older than %s", reaped, cutoff)
    except Exception as e:
        logger.warning("reap_stale_running failed (non-fatal): %s", e)
    try:
        run_id = await preallocate_run_storage(db_path, str(thread_id))
        return {"prealloc_run_id": run_id}
    except Exception as e:
        logger.warning("preallocate_run failed (non-fatal): %s", e)
        return {}


async def extract_facts(state: AgentState, config: RunnableConfig) -> dict:
    """Per-source fact extraction over the run's captured run_source rows (research path)."""
    configurable = Configuration.from_runnable_config(config)
    if not configurable.persist_results:
        return {}
    thread_id = (config.get("configurable") or {}).get("thread_id")
    if not thread_id:
        logger.warning("No thread_id found in config, skipping fact extraction.")
        return {}
    
    logger.info("Starting fact extraction for thread %s", thread_id)
    try:
        import aiosqlite
        from open_deep_research.factbase import (
            entities as fbentities,
            extractor as fbextractor,
            ingest as fbingest,
            migrations as fbmig,
            profile as fbprofile,
            registry as fbregistry,
            schema as fbschema,
            store as fbstore,
        )
        prof = fbprofile.load(configurable.profile_name)
        reg = fbregistry.SourceRegistry.load(configurable.registry_name)
        model_call = _make_fact_model_call(
            configurable, config, target_properties=state.get("target_properties"))
        # _make_fact_model_call is normally a sync factory returning an async model_call,
        # but tests (and any async factory) may return a coroutine -- await it if so.
        if asyncio.iscoroutine(model_call):
            model_call = await model_call

        run_id = state.get("prealloc_run_id")
        async with aiosqlite.connect(get_db_path(config)) as conn:
            await fbmig.apply(conn, fbschema.STEPS)
            # Provenance: stamp which profile produced this run's facts (after selection/load,
            # before extraction). Direct UPDATE within the open connection.
            if run_id:
                # Drift signal: if a *prior* run used a different hash for this profile, warn
                # (warn-and-proceed). The current run isn't stamped yet, so exclude its id.
                _cur = await conn.execute(
                    "SELECT profile_hash FROM research_runs "
                    "WHERE profile_name=? AND profile_hash IS NOT NULL AND id<>? "
                    "ORDER BY id DESC LIMIT 1",
                    (configurable.profile_name, run_id))
                _prev = await _cur.fetchone()
                _cur_hash = getattr(prof, "profile_hash", None)
                if _prev and _prev[0] and _cur_hash and _prev[0] != _cur_hash:
                    logger.warning(
                        "Profile '%s' changed since the last run (%s -> %s); prior facts may be "
                        "stale until `dossier recompute --profile %s`.",
                        configurable.profile_name, _prev[0][:8], _cur_hash[:8], configurable.profile_name)
                await conn.execute(
                    "UPDATE research_runs SET profile_name=?, profile_version=?, profile_hash=? WHERE id=?",
                    (configurable.profile_name,
                     getattr(prof, "profile_version", None),
                     getattr(prof, "profile_hash", None),
                     run_id),
                )
                await conn.commit()
            from open_deep_research.factbase import backfill as _fb_backfill
            from open_deep_research.factbase import recompute as _fb_recompute
            from open_deep_research.storage import extract_sources as _extract_sources

            # Backfill canonical values on any pre-normalization rows so dedup/conflict
            # /rendering treat them consistently with newly-ingested facts (idempotent).
            if configurable.normalize_fact_values:
                await _fb_recompute.backfill_canonical_values(conn, prof)
            
            # 1. Backfill any cited sources that weren't captured during search
            cited = _extract_sources(state.get("final_report", "") or "", *(state.get("raw_notes", []) or []))
            if cited:
                logger.info("Backfilling %d cited sources for thread %s", len(cited), thread_id)
                await _fb_backfill.backfill_run_sources(
                    fbstore.RunSourceStore(conn), str(thread_id), cited, _fact_fetch_text)
            
            # 2. Read all captured sources
            sources = await fbstore.RunSourceStore(conn).read(str(thread_id))
            logger.info("Found %d sources for thread %s", len(sources), thread_id)
            
            if run_id:
                # Update coverage status
                best_status = {}
                for s in sources:
                    u, st = s["source_url"], s["capture_status"]
                    if st == "raw_text" or u not in best_status:
                        best_status[u] = st
                if any(st != "raw_text" for st in best_status.values()):
                    from open_deep_research.storage import set_coverage_incomplete
                    await set_coverage_incomplete(get_db_path(config), run_id, True)

            # 3. Extract facts from 'raw_text' sources NOT already mined this run (the
            #    facts-first loop re-extracts only newly-fetched sources -> bounded cost).
            already_extracted = set(state.get("extracted_source_urls") or [])
            valid_sources = [
                s for s in sources
                if s["capture_status"] == "raw_text" and s["text"]
                and s["source_url"] not in already_extracted
            ]
            if not valid_sources:
                logger.info("No new raw_text sources to extract for thread %s", thread_id)
                return {}

            logger.info("Extracting facts from %d sources in parallel...", len(valid_sources))
            
            async def _extract_one(s):
                try:
                    recs = await fbextractor.extract(s["text"], prof, model_call)
                    for r in recs:
                        r.setdefault("source_url", s["source_url"])
                    return recs
                except Exception as e:
                    logger.warning("Extraction failed for %s: %s", s["source_url"], e)
                    return []

            extraction_tasks = [_extract_one(s) for s in valid_sources]
            task_results = await asyncio.gather(*extraction_tasks)
            
            all_records = []
            for recs in task_results:
                all_records.extend(recs)
            
            logger.info("Extracted %d total facts from %d sources.", len(all_records), len(valid_sources))
            
            # 4. Ingest extracted facts into the factbase
            if all_records and run_id:
                logger.info("Ingesting %d facts into factbase for run %d", len(all_records), run_id)
                await fbingest.Ingestor(
                    conn,
                    profile=prof,
                    resolver=fbentities.CountryResolver(),
                    registry=reg,
                    normalize_values=configurable.normalize_fact_values,
                ).ingest(run_id=run_id, records=all_records)
            # Record which sources we mined so a facts-first gap round skips them.
            return {"extracted_source_urls": [s["source_url"] for s in valid_sources]}
    except Exception as e:
        logger.warning("extract_facts failed (non-fatal): %s", e)
        import traceback
        logger.debug(traceback.format_exc())
    return {}


def _target_property_coverage(grouped_rows, target_properties):
    """For each target property, whether the fact base has it (present) and a trusted value."""
    present = {p: False for p in target_properties}
    trusted = {p: False for p in target_properties}
    for r in grouped_rows:
        p = r.get("property_name")
        if p in present:
            present[p] = True
            if r.get("admission") == "trusted" and not r.get("in_conflict"):
                trusted[p] = True
    return present, trusted


async def assess_sufficiency(state: AgentState, config: RunnableConfig) -> Command[Literal["write_research_brief", "answer_from_facts"]]:
    """Facts-first: are the question's target properties covered? If not, loop to research the gaps.

    Routes back to write_research_brief (a gap round) when target properties are still missing and
    the round budget (max_fact_rounds) allows; otherwise to answer_from_facts.
    """
    configurable = Configuration.from_runnable_config(config)
    targets = state.get("target_properties") or []
    subject = state.get("subject")
    rounds_used = state.get("fact_rounds_used", 0) or 0

    missing = []
    if targets and subject:
        try:
            import aiosqlite
            from open_deep_research.factbase import entities as fbentities, query as fbquery
            instance_key = fbentities.CountryResolver().resolve(subject)
            if instance_key:
                async with aiosqlite.connect(get_db_path(config)) as conn:
                    grouped = await fbquery.FactQuery(conn).show_grouped(instance_key)
                present, _trusted = _target_property_coverage(grouped, targets)
                missing = [p for p in targets if not present[p]]
        except Exception as e:
            logger.warning("assess_sufficiency check failed (treating as sufficient): %s", e)

    if missing and rounds_used + 1 < configurable.max_fact_rounds:
        logger.info("Facts insufficient (missing %s); gap round %d", missing, rounds_used + 1)
        gap = (
            "The following facts are still missing and MUST be found: "
            + ", ".join(missing) + ". Search specifically for these."
        )
        return Command(
            goto="write_research_brief",
            update={"missing_information": gap, "fact_rounds_used": rounds_used + 1},
        )
    if missing:
        logger.info("Facts still missing %s but round budget exhausted; answering with what we have", missing)
    return Command(goto="answer_from_facts", update={"fact_rounds_used": rounds_used})


def _facts_answer_text(subject, grouped_rows, targets) -> str:
    """Deterministic, grounded answer from the grouped fact base: one line per target
    property (value | status | sources); missing targets flagged explicitly."""
    by_prop = {}
    for r in grouped_rows:
        by_prop.setdefault(r.get("property_name"), []).append(r)
    lines = [f"# {subject or 'Subject'} — facts"]
    for p in (targets or sorted(by_prop)):
        rows = by_prop.get(p)
        if not rows:
            lines.append(f"- **{p}**: missing — not found in sources.")
            continue
        for r in rows:
            status = "trusted" if (r.get("admission") == "trusted" and not r.get("in_conflict")) else \
                ("in-conflict" if r.get("in_conflict") else "provisional")
            lines.append(f"- **{p}**: {r.get('value')} ({status}, {r.get('source_count', 0)} sources)")
    return "\n".join(lines)


async def answer_from_facts(state: AgentState, config: RunnableConfig) -> dict:
    """Facts-first: answer the question directly from the structured fact base (no prose report)."""
    configurable = Configuration.from_runnable_config(config)
    subject = state.get("subject")
    question = get_buffer_string(state.get("messages", []))
    targets = state.get("target_properties") or []

    import aiosqlite
    from open_deep_research.factbase import entities as fbentities, query as fbquery
    instance_key = fbentities.CountryResolver().resolve(subject) if subject else None
    grouped = []
    if instance_key:
        async with aiosqlite.connect(get_db_path(config)) as conn:
            grouped = await fbquery.FactQuery(conn).show_grouped(instance_key)
    if targets:
        grouped = [r for r in grouped if r.get("property_name") in targets]

    deterministic = _facts_answer_text(subject, grouped, targets)

    # Optional cheap-LLM polish, grounded ONLY in the deterministic facts (best-effort).
    answer = deterministic
    try:
        polish_model_name = configurable.facts_answer_polish_model or configurable.summarization_model
        polish_model = configurable_model.with_config({
            "model": polish_model_name,
            "max_tokens": configurable.summarization_model_max_tokens,
            "api_key": get_api_key_for_model(polish_model_name, config),
            "tags": ["langsmith:nostream"],
        })
        resp = await polish_model.ainvoke([HumanMessage(content=facts_answer_polish_prompt.format(
            question=question, facts=deterministic))])
        polished = str(resp.content).strip()
        if polished:
            answer = polished
    except Exception as e:
        logger.warning("facts answer polish failed; using deterministic answer: %s", e)

    return {
        "final_report": answer,
        "messages": [AIMessage(content=answer)],
        "subject": subject,
    }


# Main Deep Researcher Graph Construction
# Creates the complete deep research workflow from user input to final report
deep_researcher_builder = StateGraph(
    AgentState,
    input=AgentInputState,
    config_schema=Configuration
)

# Add main workflow nodes for the complete research process
deep_researcher_builder.add_node("preallocate_run", preallocate_run)               # Preallocate run id for shared fact capture
deep_researcher_builder.add_node("assess_knowledge", assess_knowledge)             # Entry: subject match + knowledge decision
deep_researcher_builder.add_node("answer_from_dossier", answer_from_dossier)       # Answer directly from stored knowledge
deep_researcher_builder.add_node("clarify_with_user", clarify_with_user)           # User clarification phase
deep_researcher_builder.add_node("write_research_brief", write_research_brief)     # Research planning phase
deep_researcher_builder.add_node("research_supervisor", supervisor_subgraph)       # Research execution phase
deep_researcher_builder.add_node("final_report_generation", final_report_generation)  # Report generation phase
deep_researcher_builder.add_node("extract_facts", extract_facts)                   # Per-source fact extraction (research path)
deep_researcher_builder.add_node("assess_sufficiency", assess_sufficiency)         # Facts-first: enough to answer?
deep_researcher_builder.add_node("answer_from_facts", answer_from_facts)           # Facts-first: answer from the fact base
deep_researcher_builder.add_node("persist_research", persist_research)             # Persist results to SQLite


def route_after_research(state: AgentState, config: RunnableConfig) -> str:
    """Facts-first mode skips the prose report and goes straight to fact extraction."""
    return "extract_facts" if Configuration.from_runnable_config(config).facts_first_mode \
        else "final_report_generation"


def route_after_extract(state: AgentState, config: RunnableConfig) -> str:
    """Facts-first mode checks sufficiency next; report mode persists as before."""
    return "assess_sufficiency" if Configuration.from_runnable_config(config).facts_first_mode \
        else "persist_research"


# Define main workflow edges. assess_knowledge (entry) branches via Command(goto)
# to answer_from_dossier / write_research_brief / clarify_with_user; assess_sufficiency
# branches via Command(goto) to write_research_brief (gap round) / answer_from_facts.
deep_researcher_builder.add_edge(START, "preallocate_run")                          # Entry point: preallocate the run id
deep_researcher_builder.add_edge("preallocate_run", "assess_knowledge")             # Then check the knowledge base
deep_researcher_builder.add_edge("answer_from_dossier", "persist_research")         # Cached answer -> log the run
deep_researcher_builder.add_edge("write_research_brief", "research_supervisor")     # Brief to research
deep_researcher_builder.add_conditional_edges(                                      # Research -> report (default) | facts (facts-first)
    "research_supervisor", route_after_research,
    {"final_report_generation": "final_report_generation", "extract_facts": "extract_facts"})
deep_researcher_builder.add_edge("final_report_generation", "extract_facts")       # Report to fact extraction
deep_researcher_builder.add_conditional_edges(                                      # Facts -> persist (default) | sufficiency (facts-first)
    "extract_facts", route_after_extract,
    {"persist_research": "persist_research", "assess_sufficiency": "assess_sufficiency"})
deep_researcher_builder.add_edge("answer_from_facts", "persist_research")           # Facts answer -> persist
deep_researcher_builder.add_edge("persist_research", END)                          # Final exit point

# Compile the complete deep researcher workflow
deep_researcher = deep_researcher_builder.compile()