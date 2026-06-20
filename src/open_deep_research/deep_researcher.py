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
from open_deep_research.failover import discard_tracker, get_tracker, new_run_tracker
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
    profile_selection_prompt,
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
    SelectedProfile,
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

from open_deep_research.nodes.common import (
    _report_is_failed,
    _is_empty_run,
    _run_fact_count,
    _raw_text_source_count,
    _fact_fetch_text,
    recommended_recursion_limit,
    COMPRESSION_FAILED_SENTINEL,
    ALL_RESEARCH_FAILED_SENTINEL,
    REPORT_FAILED_PREFIX,
)
from open_deep_research.nodes.profiles import (
    select_profile,
    resolve_target_properties,
    _effective_profile_name,
    _resolve_subject,
)
from open_deep_research.nodes.report import final_report_generation


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
        "model_chain": configurable.model_chain("supervisor"),
        "stage": "supervisor",
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
                "model_chain": configurable.model_chain("supervisor"),
                "stage": "supervisor",
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
        "model_chain": configurable.model_chain("final_report"),
        "stage": "final_report",
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


def _steer_brief_with_catalog(research_brief: str, prof, target_properties: list) -> str:
    """Augment a facts-first research brief with the profile's compiled property catalog.

    Steering with bare property NAMES under-specifies the research: the researcher isn't told
    a property's definition, allowed values, or required qualifiers, so it gathers loose
    variants and values without the qualifiers extraction needs (e.g. a coverage % with no
    population basis). Injecting ``compile_property_catalog`` -- the same definitions/qualifiers
    used for extraction -- tells the researcher exactly what to find, and to capture qualifiers.
    """
    from open_deep_research.factbase.prompting import compile_property_catalog
    catalog = compile_property_catalog(prof, target_properties)
    return (
        f"{research_brief}\n\nGather the specific facts needed to answer this. For each "
        f"property below, find a cited value that matches its definition and allowed values, "
        f"and capture any listed qualifier the sources state (e.g. the population basis for a "
        f"coverage percentage):\n{catalog}"
    )


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

    # Query-driven profile selection: pick the best-matching domain profile once per run (a
    # gap round re-enters this node, so reuse the already-selected one). Threaded via state so
    # extract_facts uses the same profile. Falls back to configurable.profile_name.
    selected_profile_name = state.get("selected_profile_name")
    if configurable.auto_select_profile and not selected_profile_name:
        selected_profile_name = await select_profile(question, configurable, config)
    profile_name = selected_profile_name or configurable.profile_name

    # Facts-first answers/sufficiency resolve the fact-base instance from `subject`, but on
    # the research path subject is otherwise only resolved at persist time (and not at all
    # when the KB is off). Resolve it here so the facts nodes have an instance to query.
    if (configurable.facts_first_mode or configurable.whole_profile_mode) and not subject:
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
                "model_chain": configurable.model_chain("supervisor"),
                "stage": "supervisor",
                "max_tokens": configurable.researcher_model_max_tokens,
                "api_key": get_api_key_for_model(configurable.supervisor_model, config),
                "tags": ["langsmith:nostream"],
            })
        )
        response = await supervisor_model.ainvoke([HumanMessage(content=transform_messages_into_research_topic_prompt.format(
            messages=question, date=get_today_str()
        ))])
        research_brief = response.research_brief

    # Facts-first / whole-profile: resolve which fact properties to target and steer research.
    target_properties = state.get("target_properties")
    if configurable.facts_first_mode or configurable.whole_profile_mode:
        from open_deep_research.factbase import profile as _fbprofile
        _prof = _fbprofile.load(profile_name)
        if configurable.whole_profile_mode and not target_properties:
            # Round 1 covers the whole profile. On gap rounds, assess_completeness has narrowed
            # target_properties to the still-incomplete set -- keep it (don't re-target resolved
            # properties), so steering + extraction focus on what's actually missing.
            target_properties = [pd.name for pd in _prof.properties]
        elif not configurable.whole_profile_mode and not target_properties:
            target_properties = await resolve_target_properties(
                question, _prof, configurable, config
            )
        if target_properties:
            # Steer research with the property catalog (definitions, allowed values,
            # qualifiers) -- not just bare names -- so facts are gathered with their qualifiers.
            research_brief = _steer_brief_with_catalog(research_brief, _prof, target_properties)

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
    if selected_profile_name:
        update["selected_profile_name"] = selected_profile_name
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

async def _merge_dossier(subject, existing_report, new_report, configurable, config):
    """Merge a new report into a subject's existing dossier (preserve + integrate)."""
    model = configurable_model.with_config({
        "model": configurable.final_report_model,
        "model_chain": configurable.model_chain("final_report"),
        "stage": "final_report",
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


async def _facts_report_md(config, instance_key) -> str:
    """Render the facts gathered for an instance as dossier show-style markdown (NO LLM)."""
    import aiosqlite
    from open_deep_research.factbase import (query as _fbq, render as _fbr,
                                             schema as _fbschema, migrations as _fbmig)
    from open_deep_research.storage import _ensure_schema as _ens
    async with aiosqlite.connect(get_db_path(config)) as conn:
        await _ens(conn)
        await _fbmig.apply(conn, _fbschema.STEPS)
        grouped = await _fbq.FactQuery(conn).show_grouped(instance_key)
    return _fbr.render(grouped, fmt="md") if grouped else ""


async def _checkpoint_dossier(state, config) -> None:
    """Persist a PARTIAL subject dossier from the facts gathered so far (no LLM), so a
    whole-profile run that aborts/times out mid-loop still saves a usable dossier rather than
    nothing. Guards: requires an already-set subject (skip LLM resolution), fact_count>0, and a
    brand-new subject (never overwrites an existing established dossier). Best-effort."""
    try:
        subject = state.get("subject")
        if not subject:
            return
        db_path = get_db_path(config)
        prealloc = state.get("prealloc_run_id")
        fact_count = await _run_fact_count(db_path, prealloc) if prealloc else 0
        if fact_count <= 0:                                   # Guard 1
            return
        slug = slugify(subject)
        existing = await get_subject_by_slug(db_path, slug)
        if existing and existing.get("current_report"):       # Guard 2: don't poison existing
            return
        from open_deep_research.factbase import entities as _fbe
        ik = _fbe.CountryResolver().resolve_in_text(subject)
        if not ik:
            return
        report = await _facts_report_md(config, ik)
        if not report.strip():
            return
        now = datetime.now(timezone.utc).isoformat()
        sources = extract_sources(report)
        run = {
            "thread_id": (config.get("configurable") or {}).get("thread_id"),
            "topic": subject, "research_brief": state.get("research_brief"),
            "final_report": report, "sources": sources, "raw_notes": state.get("raw_notes", []),
            "config": {}, "status": "partial", "error": None, "created_at": now,
        }
        await save_run_and_upsert_subject(
            db_path, subject_name=subject, slug=slug, merged_report=report,
            sources_union=sources, run=run, now=now, run_id=prealloc)
        logger.info("Checkpointed partial dossier for %s (%d facts).", subject, fact_count)
    except Exception as e:  # noqa: BLE001 - best-effort; never fail the run on a checkpoint
        logger.warning("Partial-dossier checkpoint failed (non-fatal): %s", e)


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
    _thread_id = (config.get("configurable") or {}).get("thread_id")
    config_used["failovers"] = [f.as_dict() for f in get_tracker(_thread_id).failovers]
    discard_tracker(_thread_id)

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
            return {"report_id": run_id, "subject": subject_for_log,
                    "fact_count": 0, "status": "error"}

        # Empty-run gate: a run that captured no raw_text sources AND extracted no facts is a
        # failed research attempt (the Brazil class), not a real dossier. Log it as an error so
        # the batch ledger retries it on resume -- never merge it into the subject dossier.
        # Scoped to dossier/facts mode only: a normal report-mode run legitimately produces 0
        # facts and must still be persisted.
        thread_id = (config.get("configurable") or {}).get("thread_id")
        prealloc = state.get("prealloc_run_id")
        fact_count = await _run_fact_count(db_path, prealloc) if prealloc else 0
        src_count = await _raw_text_source_count(db_path, thread_id) if thread_id else 0
        dossier_mode = configurable.facts_first_mode or configurable.whole_profile_mode
        if dossier_mode and _is_empty_run(fact_count=fact_count, raw_text_source_count=src_count):
            run["status"] = "error"
            run["error"] = "empty run: 0 facts, 0 raw_text sources"
            subject_for_log = state.get("subject") or topic
            run_id = await log_research_run(db_path, slugify(subject_for_log), run,
                                            run_id=state.get("prealloc_run_id"))
            logger.error("Empty run (0 facts/0 sources); logged as error for retry.")
            return {"report_id": run_id, "subject": subject_for_log,
                    "fact_count": 0, "status": "error"}

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
        return {"report_id": run_id, "subject": subject_name,
                "fact_count": fact_count, "status": "completed"}
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
    # Free-text context the source gives around this value (1-3 sentences): caveats,
    # scope, methodology, or qualitative detail that the structured value alone omits.
    narrative: Optional[str] = None


class ExtractionResult(BaseModel):
    """List of facts extracted from a single source."""

    facts: list[FactRecord] = Field(default_factory=list)


def _make_fact_model_call(configurable, config, target_properties=None):
    """Build an async model_call(source_text, prof) -> list[dict] for the extractor.

    Invokes the model as plain text and parses leniently via parse_lean_facts, so a
    cheap model can emit a JSON array without needing structured-output scaffolding.
    Best-effort: returns [] on any error so extraction never fails a completed run.
    ``target_properties`` (facts-first mode) narrows extraction to the properties the
    question needs; default = all profile properties.
    """
    async def model_call(source_text, prof):
        try:
            from open_deep_research.factbase.lean_extract import parse_lean_facts
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
                .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
                .with_config({
                    "model": extraction_model,
                    "model_chain": configurable.model_chain("researcher", "extract_facts"),
                    "stage": "extract_facts",
                    "max_tokens": configurable.researcher_model_max_tokens,
                    "api_key": get_api_key_for_model(configurable.researcher_model, config),
                    "tags": ["langsmith:nostream"],
                })
            )
            resp = await model.ainvoke([HumanMessage(content=prompt)])
            return parse_lean_facts(str(getattr(resp, "content", "") or ""))
        except Exception as e:
            logger.warning("fact model_call failed (non-fatal): %s", e)
            return []
    return model_call


async def _maybe_propose_extensions(configurable, config, prof, profile_name, source_texts) -> None:
    """Ask the model for valuable facts the profile doesn't model; append them to a draft.

    Reuses the assisted-scaffolding path (``scaffold.induce`` proposes only NEW properties,
    validated against the profile meta-schema) seeded with this run's source text. The result
    is merged into ``<profile_name>.extension.draft.yaml`` for a human to review and merge --
    the production profile is never touched. Best-effort; the caller swallows exceptions.
    """
    from open_deep_research.factbase import scaffold as fbscaffold

    if not source_texts:
        return
    existing_names = [pd.name for pd in prof.properties]
    description = f"facts worth gathering about a {prof.entity_type} (profile '{profile_name}')"

    async def _model_call(prompt):
        model = (
            configurable_model
            .with_structured_output(fbscaffold.ScaffoldProposal)
            .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
            .with_config({
                # ScaffoldProposal is a complex nested schema (like ExtractionResult): route it
                # to the propose_extensions step (gemini-2.5-pro primary) so flash doesn't keep
                # failing structured-output validation and burning the Claude fallback.
                "model": configurable.model_for("propose_extensions", "summarization"),
                "model_chain": configurable.model_chain("summarization", "propose_extensions"),
                "stage": "propose_extensions",
                "max_tokens": configurable.summarization_model_max_tokens,
                "api_key": get_api_key_for_model(configurable.model_for("propose_extensions", "summarization"), config),
                "tags": ["langsmith:nostream"],
            })
        )
        return await model.ainvoke([HumanMessage(content=prompt)])

    # Cap seed sources to bound prompt size (build_scaffold_prompt also truncates each).
    proposal = await fbscaffold.induce(
        prof.entity_type, description, source_texts[:8], existing_names, _model_call)
    path, added = fbscaffold.write_extension_draft(profile_name, prof.entity_type, proposal)
    if added:
        logger.info("Proposed %d profile extension(s) for '%s' -> %s: %s",
                    len(added), profile_name, path, ", ".join(added))
    else:
        logger.info("No new profile extensions proposed for '%s'", profile_name)


async def preallocate_run(state: AgentState, config: RunnableConfig) -> dict:
    """Create the research_runs row early so the tool layer/extract_facts share a run id."""
    thread_id = (config.get("configurable") or {}).get("thread_id")
    tracker = new_run_tracker(thread_id)  # fresh per-run failover state keyed by thread_id for cross-node visibility
    try:
        from open_deep_research.preflight import run_preflight
        from open_deep_research.model_routing import load_routing
        run_preflight(load_routing(), tracker)
    except Exception as e:  # PreflightError (fail policy) or unexpected probe error
        from open_deep_research.preflight import PreflightError
        if isinstance(e, PreflightError):
            raise
        logger.warning("preflight skipped due to probe error: %s", e)
    configurable = Configuration.from_runnable_config(config)
    if not configurable.persist_results:
        return {}
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
        profile_name = _effective_profile_name(state, configurable)
        prof = fbprofile.load(profile_name)
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
                    (profile_name, run_id))
                _prev = await _cur.fetchone()
                _cur_hash = getattr(prof, "profile_hash", None)
                if _prev and _prev[0] and _cur_hash and _prev[0] != _cur_hash:
                    logger.warning(
                        "Profile '%s' changed since the last run (%s -> %s); prior facts may be "
                        "stale until `dossier recompute --profile %s`.",
                        profile_name, _prev[0][:8], _cur_hash[:8], profile_name)
                await conn.execute(
                    "UPDATE research_runs SET profile_name=?, profile_version=?, profile_hash=? WHERE id=?",
                    (profile_name,
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

            sem = asyncio.Semaphore(int(os.getenv("EXTRACT_FACTS_CONCURRENCY",
                                                   str(configurable.max_concurrent_research_units or 4))))
            _extraction_errors = []

            async def _extract_one(s):
                async with sem:
                    try:
                        recs = await fbextractor.extract(s["text"], prof, model_call)
                        for r in recs:
                            r.setdefault("source_url", s["source_url"])
                        return recs
                    except Exception as e:
                        logger.warning("Extraction failed for %s: %s", s["source_url"], e)
                        _extraction_errors.append(s["source_url"])
                        return []

            extraction_tasks = [_extract_one(s) for s in valid_sources]
            task_results = await asyncio.gather(*extraction_tasks)

            errs = len(_extraction_errors)
            if errs:
                logger.warning(
                    "extract_facts: %d/%d sources failed extraction",
                    errs, len(valid_sources),
                )

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

            # 5. Opportunistically propose profile extensions for valuable facts the profile
            #    doesn't capture (draft file only; never edits the production profile).
            if configurable.propose_profile_extensions:
                try:
                    await _maybe_propose_extensions(
                        configurable, config, prof, profile_name,
                        [s["text"] for s in valid_sources if s.get("text")],
                    )
                except Exception as e:
                    logger.warning("profile-extension proposal failed (non-fatal): %s", e)

            # Record which sources we mined so a facts-first gap round skips them.
            result = {"extracted_source_urls": [s["source_url"] for s in valid_sources]}
            if errs:
                result["extraction_errors"] = errs
            return result
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
            logger.warning("assess_sufficiency check failed (treating as still-missing): %s", e)
            missing = list(targets)

    if missing and rounds_used + 1 < configurable.max_fact_rounds:
        logger.info("Facts insufficient (missing %s); gap round %d", missing, rounds_used + 1)
        gap = (
            "The following facts are still missing and MUST be found: "
            + ", ".join(missing) + ". Search specifically for these."
        )
        return Command(
            goto="write_research_brief",
            update={"missing_information": gap, "fact_rounds_used": rounds_used + 1,
                    "target_properties": missing},
        )
    if missing:
        logger.info("Facts still missing %s but round budget exhausted; answering with what we have", missing)
    return Command(goto="answer_from_facts", update={"fact_rounds_used": rounds_used})


def _gaploop_decision(incomplete, prev_incomplete, rounds_used, max_rounds):
    """Pure whole-profile gap-loop routing decision (no I/O).

    Returns ``(goto, no_progress)``:
      - ``goto``: "write_research_brief" for another gap round, else "synthesize_narrative" (finalize).
      - ``no_progress``: True when a gap round (rounds_used >= 1) closed ZERO gaps -- the
        still-incomplete required-property set is unchanged from the prior round. ``incomplete``
        only stays-same or shrinks across rounds, so set-equality is a valid no-progress test.

    Bail-out: the first no-progress gap round finalizes instead of looping (aggressive threshold).
    """
    no_progress = (
        rounds_used >= 1
        and prev_incomplete is not None
        and set(incomplete) == set(prev_incomplete)
    )
    if incomplete and not no_progress and rounds_used + 1 < max_rounds:
        return "write_research_brief", no_progress
    return "synthesize_narrative", no_progress


async def assess_completeness(state: AgentState, config: RunnableConfig) -> Command[Literal["write_research_brief", "synthesize_narrative"]]:
    """Whole-profile: loop until every REQUIRED property is resolved-or-confirmed-absent or budget hit.

    Routes to write_research_brief (gap round) while any required property is incomplete and
    the round budget (max_profile_rounds) allows; otherwise routes to synthesize_narrative (Task 7).
    """
    import aiosqlite
    from open_deep_research.factbase import (
        entities as fbentities,
        query as fbquery,
        profile as fbprofile,
        completeness as fbc,
        schema as fbschema,
        migrations as fbmig,
    )
    from open_deep_research.factbase.property_status import PropertyStatusStore

    configurable = Configuration.from_runnable_config(config)
    subject = state.get("subject")
    rounds_used = state.get("fact_rounds_used", 0) or 0
    prof = fbprofile.load(_effective_profile_name(state, configurable))

    ik = fbentities.CountryResolver().resolve_in_text(subject) if subject else None
    if not ik:
        # Can't resolve subject to a country — go straight to terminal
        return Command(goto="synthesize_narrative", update={"fact_rounds_used": rounds_used})

    # Persist a partial dossier from the facts gathered so far (cheap, no LLM) BEFORE the
    # loop/finalize decision, so a run aborted/timed-out in a later gap round still saved a
    # usable dossier (the empty-dossier-on-timeout failure). Best-effort.
    await _checkpoint_dossier(state, config)

    notes_text = "\n".join(state.get("raw_notes", []) or [])[:8000]

    async with aiosqlite.connect(get_db_path(config)) as conn:
        await fbmig.apply(conn, fbschema.STEPS)
        store = PropertyStatusStore(conn)
        absent = await store.absent_properties(ik)
        grouped = await fbquery.FactQuery(conn).show_grouped(ik)
        ledger = fbc.assess_property_status(grouped, absent, prof)

        # Affirmative-absence pass for still-missing REQUIRED properties (bounded by this round).
        model_call = _make_absence_judge_call(configurable, config)
        for pd in prof.properties:
            if (pd.completeness == "required"
                    and ledger.get(pd.name) == "missing_value"
                    and getattr(pd, "absence_allowed", False)
                    and pd.name not in absent):
                if await judge_absence(pd.name, pd.description, notes_text, model_call):
                    await store.record_absent(
                        ik, pd.name, {}, "no data after targeted search",
                        state.get("prealloc_run_id"), None,
                    )
                    ledger[pd.name] = "confirmed_absent"

        await conn.commit()

    incomplete = [
        pd.name for pd in prof.properties
        if pd.completeness == "required" and not fbc.is_complete(ledger.get(pd.name, "missing_value"), pd)
    ]
    goto, no_progress = _gaploop_decision(
        incomplete, state.get("prev_incomplete_props"), rounds_used, configurable.max_profile_rounds
    )
    if goto == "write_research_brief":
        logger.info("Whole-profile incomplete (%s); gap round %d", incomplete, rounds_used + 1)
        gap = (
            "These profile properties are still incomplete and MUST be resolved or, if no data "
            "exists, explicitly confirmed unavailable after searching: "
            + ", ".join(f"{p} ({ledger.get(p)})" for p in incomplete) + "."
        )
        return Command(
            goto="write_research_brief",
            update={"missing_information": gap, "target_properties": incomplete,
                    "fact_rounds_used": rounds_used + 1,
                    "prev_incomplete_props": incomplete},
        )
    if no_progress:
        logger.info("Gap round closed zero gaps (%s unchanged); bailing out to finalize", incomplete)
    elif incomplete:
        logger.info("Whole-profile still incomplete %s but round budget exhausted; finishing", incomplete)
    return Command(goto="synthesize_narrative", update={"fact_rounds_used": rounds_used})


def _best_singular_row(rows: list) -> dict:
    """Pick the single best row for a singular property: most-corroborated, then prefer a
    non-conflicting value, then the longest (most specific) value. Deterministic.

    Sources name the same scheme many ways ("e-ID" / "electronic identification" / "personal
    ID code"); text canonicalization can't merge generic synonyms (and value_aliases are
    property-global, so aliasing them would corrupt other countries), so a singular property
    collapses to its best value at render time rather than dumping every variant.
    """
    return max(rows, key=lambda r: (
        r.get("source_count", 0),
        1 if r.get("admission") == "trusted" else 0,   # a trusted value beats a provisional one
        0 if r.get("in_conflict") else 1,
        len(str(r.get("value") or "")),
    ))


def _display_value(row: dict) -> str:
    """A readable value for the answer: a raw surface form from ``variants`` (the longest, most
    complete one), not the noise-stripped canonical that ``group_by_canonical`` puts in ``value``
    (e.g. show "Estonia's digital ID", not the canonical "estonia s digital")."""
    variants = [v for v in (row.get("variants") or []) if v and v.strip()]
    if variants:
        return max(variants, key=len)
    return str(row.get("value") or "")


class AbsenceJudgement(BaseModel):
    """Whether a property is genuinely absent for the subject after a targeted search."""

    absent: bool


async def judge_absence(prop_name, prop_desc, notes_text, model_call) -> bool:
    """True only if the model affirms no data exists for this property after searching.

    Best-effort: any error -> False (treat as still-missing, keep trying within budget).
    """
    try:
        res = await model_call(prop_name, prop_desc, notes_text)
        return bool(getattr(res, "absent", False))
    except Exception as e:  # noqa: BLE001
        logger.warning("absence judge failed (non-fatal) for %s: %s", prop_name, e)
        return False


class NameConsolidation(BaseModel):
    """Whether several extracted name-variants denote the SAME entity, and the best name."""

    same_entity: bool
    canonical_name: str = ""


async def _consolidate_name_group(subject, prop_name, prop_desc, rows, model_call):
    """Merge name-variants that denote the same entity into one row, via a best-effort model.

    Deterministic canonicalization can't merge synonyms ("e-ID" / "electronic identification"
    / "Digi-ID" are the same scheme); this asks the model whether the distinct variants name
    the same thing and, if so, returns ONE merged row using the model's canonical name (with
    corroboration summed). Returns None (keep variants as-is) when there is <2 to merge, the
    model says they differ, or anything fails -- so the deterministic path still applies.
    """
    values = []
    for r in rows:
        v = _display_value(r)
        if v and v not in values:
            values.append(v)
    if len(values) < 2:
        return None
    try:
        result = await model_call(subject, prop_name, prop_desc, values)
    except Exception as e:  # noqa: BLE001 - consolidation is best-effort
        logger.warning("name consolidation failed (non-fatal) for %s: %s", prop_name, e)
        return None
    if not (result and getattr(result, "same_entity", False)
            and (getattr(result, "canonical_name", "") or "").strip()):
        return None
    canonical = result.canonical_name.strip()
    best = _best_singular_row(rows)
    return {
        **best,
        "value": canonical,
        "variants": [canonical],
        "source_count": sum(int(r.get("source_count") or 0) for r in rows),
        "in_conflict": False,
        "admission": "trusted" if any(r.get("admission") == "trusted" for r in rows)
        else best.get("admission"),
    }


def _make_name_consolidation_call(configurable, config):
    """An async ``model_call(subject, prop_name, prop_desc, values) -> NameConsolidation``
    on the cheap summarization chain, grounded strictly in the provided values."""
    async def model_call(subject, prop_name, prop_desc, values):
        model = (
            configurable_model
            .with_structured_output(NameConsolidation)
            .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
            .with_config({
                "model": configurable.summarization_model,
                "model_chain": configurable.model_chain("summarization"),
                "stage": "summarization",
                "max_tokens": configurable.summarization_model_max_tokens,
                "api_key": get_api_key_for_model(configurable.summarization_model, config),
                "tags": ["langsmith:nostream"],
            })
        )
        listing = "; ".join(f'"{v}"' for v in values)
        prompt = (
            f"Different sources gave these values for the '{prop_name}' of {subject}.\n"
            f"Property meaning: {prop_desc}\nValues: {listing}\n\n"
            f"Do these all refer to the SAME {prop_name} (the same scheme/entity), just named "
            f"differently? If yes, set same_entity=true and canonical_name to the single best, "
            f"most official, commonly-used name -- choose from or lightly normalise the given "
            f"values; do NOT invent new information. If they are genuinely different things, "
            f"set same_entity=false."
        )
        return await model.ainvoke([HumanMessage(content=prompt)])
    return model_call


def _make_absence_judge_call(configurable, config):
    """An async ``model_call(prop_name, prop_desc, notes_text) -> AbsenceJudgement``
    on the cheap summarization chain; returns absent=True only if the notes confirm
    no data exists for this property (not just that it was not covered yet)."""
    async def model_call(prop_name, prop_desc, notes_text):
        model = (
            configurable_model
            .with_structured_output(AbsenceJudgement)
            .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
            .with_config({
                "model": configurable.summarization_model,
                "model_chain": configurable.model_chain("summarization"),
                "stage": "summarization",
                "max_tokens": configurable.summarization_model_max_tokens,
                "api_key": get_api_key_for_model(configurable.summarization_model, config),
                "tags": ["langsmith:nostream"],
            })
        )
        prompt = (
            f"Research notes about a subject are below. For the property '{prop_name}' "
            f"({prop_desc}), did the research look for it and find that NO data exists / it "
            f"is not applicable? Set absent=true ONLY if the notes show a genuine, searched "
            f"absence; set absent=false if it simply wasn't covered yet.\n\nNOTES:\n{notes_text}"
        )
        return await model.ainvoke([HumanMessage(content=prompt)])
    return model_call


def _facts_answer_text(subject, grouped_rows, targets, singular_props=None) -> str:
    """Deterministic, grounded answer from the grouped fact base: one line per target
    property (value | status | sources); missing targets flagged explicitly.

    ``singular_props`` names properties that hold a single value (non-``multi``); for those,
    multiple source-variants are collapsed to the single best row (see ``_best_singular_row``)
    instead of listing every variant.
    """
    singular = set(singular_props or ())
    by_prop = {}
    for r in grouped_rows:
        by_prop.setdefault(r.get("property_name"), []).append(r)
    lines = [f"# {subject or 'Subject'} — facts"]
    for p in (targets or sorted(by_prop)):
        rows = by_prop.get(p)
        if not rows:
            lines.append(f"- **{p}**: missing — not found in sources.")
            continue
        if p in singular and len(rows) > 1:
            rows = [_best_singular_row(rows)]
        for r in rows:
            status = "trusted" if (r.get("admission") == "trusted" and not r.get("in_conflict")) else \
                ("in-conflict" if r.get("in_conflict") else "provisional")
            lines.append(f"- **{p}**: {_display_value(r)} ({status}, {r.get('source_count', 0)} sources)")
            narrative = (r.get("narrative") or "").strip()
            if narrative:
                lines.append(f"  - {narrative}")
    return "\n".join(lines)


async def _synthesize_dossier(subject, grouped, absent, overview_sections, model_call) -> str:
    """Profile-defined subject narrative grounded ONLY in gathered facts; deterministic fallback."""
    facts_block = _facts_answer_text(subject, grouped, None)   # readable, raw-value listing
    if not overview_sections:
        return facts_block
    try:
        sections = "\n".join(f"- {s}" for s in overview_sections)
        absent_line = ("Explicitly note these have no data: " + ", ".join(sorted(absent))) if absent else ""
        prompt = (f"Write a concise dossier about {subject}. Cover EACH section below as a '## ' "
                  f"heading, grounded ONLY in the facts provided -- cite nothing not present, and "
                  f"state absences plainly. {absent_line}\n\nSECTIONS:\n{sections}\n\nFACTS:\n{facts_block}")
        resp = await model_call(prompt)
        text = str(getattr(resp, "content", "") or "").strip()
        return text or facts_block
    except Exception as e:  # noqa: BLE001
        logger.warning("narrative synthesis failed; using deterministic facts: %s", e)
        return facts_block


async def synthesize_narrative(state: AgentState, config: RunnableConfig) -> dict:
    """Whole-profile: write a profile-defined subject dossier from gathered facts + confirmed-absent set."""
    import aiosqlite
    from open_deep_research.factbase import (entities as fbentities, query as fbquery,
        profile as fbprofile)
    from open_deep_research.factbase.property_status import PropertyStatusStore
    configurable = Configuration.from_runnable_config(config)
    subject = state.get("subject")
    prof = fbprofile.load(_effective_profile_name(state, configurable))
    ik = fbentities.CountryResolver().resolve_in_text(subject) if subject else None
    grouped, absent = [], set()
    if ik:
        async with aiosqlite.connect(get_db_path(config)) as conn:
            grouped = await fbquery.FactQuery(conn).show_grouped(ik)
            absent = await PropertyStatusStore(conn).absent_properties(ik)

    async def mc(prompt):
        model_name = configurable.facts_answer_polish_model or configurable.summarization_model
        model = configurable_model.with_config({
            "model": model_name,
            "model_chain": configurable.model_chain("final_report"),
            "stage": "final_report",
            "max_tokens": configurable.final_report_model_max_tokens,
            "api_key": get_api_key_for_model(model_name, config),
            "tags": ["langsmith:nostream"],
        })
        return await model.ainvoke([HumanMessage(content=prompt)])

    answer = await _synthesize_dossier(
        subject, grouped, absent, getattr(prof, "overview_sections", []), mc)
    return {"final_report": answer, "messages": [AIMessage(content=answer)], "subject": subject}


async def answer_from_facts(state: AgentState, config: RunnableConfig) -> dict:
    """Facts-first: answer the question directly from the structured fact base (no prose report)."""
    configurable = Configuration.from_runnable_config(config)
    subject = state.get("subject")
    question = get_buffer_string(state.get("messages", []))
    targets = state.get("target_properties") or []

    import aiosqlite
    from open_deep_research.factbase import entities as fbentities, query as fbquery
    # Resolve the country from the subject PHRASE (e.g. "Estonia's digital identity scheme"),
    # not just an exact country name -- extraction stores facts under the country key (EST),
    # so the answer path must find that country inside the descriptive subject or it retrieves
    # nothing and renders every property "missing".
    instance_key = fbentities.CountryResolver().resolve_in_text(subject) if subject else None
    grouped = []
    if instance_key:
        async with aiosqlite.connect(get_db_path(config)) as conn:
            grouped = await fbquery.FactQuery(conn).show_grouped(instance_key)
    if targets:
        grouped = [r for r in grouped if r.get("property_name") in targets]

    # Singular (non-multi) properties collapse to their single best value at render time.
    singular_props = set()
    prof = None
    try:
        from open_deep_research.factbase import profile as fbprofile
        prof = fbprofile.load(_effective_profile_name(state, configurable))
        singular_props = {pd.name for pd in prof.properties if not getattr(pd, "multi", False)}
    except Exception as e:  # noqa: BLE001 - profile is best-effort here; fall back to listing all
        logger.warning("facts-answer: could not load profile for singular collapse: %s", e)

    # (C) Semantic consolidation: merge name-variants that denote the same entity (e.g.
    # "e-ID"/"electronic identification"/"Digi-ID") into one canonical value via a best-effort
    # LLM pass -- deterministic canonicalization can't merge synonyms. Falls back silently.
    if configurable.consolidate_name_values and prof is not None and subject:
        name_singular = {pd.name for pd in prof.properties
                         if not getattr(pd, "multi", False)
                         and getattr(pd, "value_kind", None) in ("name", "name_year")}
        if targets:
            name_singular &= set(targets)
        if name_singular:
            model_call = _make_name_consolidation_call(configurable, config)
            by_prop = {}
            for r in grouped:
                by_prop.setdefault(r.get("property_name"), []).append(r)
            for p in name_singular:
                rows = by_prop.get(p) or []
                if len(rows) <= 1:
                    continue
                try:
                    desc = getattr(prof.property(p), "description", "") or ""
                except Exception:  # noqa: BLE001
                    desc = ""
                merged = await _consolidate_name_group(subject, p, desc, rows, model_call)
                if merged:
                    grouped = [r for r in grouped if r.get("property_name") != p] + [merged]

    deterministic = _facts_answer_text(subject, grouped, targets, singular_props=singular_props)

    # Optional cheap-LLM polish, grounded ONLY in the deterministic facts (best-effort).
    answer = deterministic
    try:
        polish_model_name = configurable.facts_answer_polish_model or configurable.summarization_model
        polish_model = configurable_model.with_config({
            "model": polish_model_name,
            "model_chain": configurable.model_chain("facts_answer_polish"),
            "stage": "facts_answer_polish",
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
deep_researcher_builder.add_node("assess_completeness", assess_completeness)       # Whole-profile: completeness loop
deep_researcher_builder.add_node("answer_from_facts", answer_from_facts)           # Facts-first: answer from the fact base
deep_researcher_builder.add_node("synthesize_narrative", synthesize_narrative)     # Whole-profile: profile-defined dossier
deep_researcher_builder.add_node("persist_research", persist_research)             # Persist results to SQLite


def route_after_research(state: AgentState, config: RunnableConfig) -> str:
    """Facts-first or whole-profile mode skips the prose report and goes straight to fact extraction."""
    configurable = Configuration.from_runnable_config(config)
    return "extract_facts" if (configurable.facts_first_mode or configurable.whole_profile_mode) \
        else "final_report_generation"


def route_after_extract(state: AgentState, config: RunnableConfig) -> str:
    """Whole-profile mode goes to completeness check; facts-first to sufficiency; else persist."""
    configurable = Configuration.from_runnable_config(config)
    if configurable.whole_profile_mode:
        return "assess_completeness"
    if configurable.facts_first_mode:
        return "assess_sufficiency"
    return "persist_research"


# Define main workflow edges. assess_knowledge (entry) branches via Command(goto)
# to answer_from_dossier / write_research_brief / clarify_with_user; assess_sufficiency
# branches via Command(goto) to write_research_brief (gap round) / answer_from_facts;
# assess_completeness branches via Command(goto) to write_research_brief / synthesize_narrative.
deep_researcher_builder.add_edge(START, "preallocate_run")                          # Entry point: preallocate the run id
deep_researcher_builder.add_edge("preallocate_run", "assess_knowledge")             # Then check the knowledge base
deep_researcher_builder.add_edge("answer_from_dossier", "persist_research")         # Cached answer -> log the run
deep_researcher_builder.add_edge("write_research_brief", "research_supervisor")     # Brief to research
deep_researcher_builder.add_conditional_edges(                                      # Research -> report (default) | facts (facts-first)
    "research_supervisor", route_after_research,
    {"final_report_generation": "final_report_generation", "extract_facts": "extract_facts"})
deep_researcher_builder.add_edge("final_report_generation", "extract_facts")       # Report to fact extraction
deep_researcher_builder.add_conditional_edges(                                      # Facts -> persist (default) | sufficiency (facts-first) | completeness (whole-profile)
    "extract_facts", route_after_extract,
    {"persist_research": "persist_research", "assess_sufficiency": "assess_sufficiency",
     "assess_completeness": "assess_completeness"})
deep_researcher_builder.add_edge("answer_from_facts", "persist_research")           # Facts answer -> persist
deep_researcher_builder.add_edge("synthesize_narrative", "persist_research")        # Narrative dossier -> persist
deep_researcher_builder.add_edge("persist_research", END)                          # Final exit point

# Compile the complete deep researcher workflow
deep_researcher = deep_researcher_builder.compile()