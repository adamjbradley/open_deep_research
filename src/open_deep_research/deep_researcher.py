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
from open_deep_research.nodes.extraction import (
    FactRecord,
    ExtractionResult,
    _make_fact_model_call,
    _maybe_propose_extensions,
    preallocate_run,
    extract_facts,
)
from open_deep_research.nodes.synthesis import (
    synthesize_narrative,
    answer_from_facts,
    _facts_answer_text,
    _synthesize_dossier,
    _best_singular_row,
    _display_value,
    NameConsolidation,
    _consolidate_name_group,
    _make_name_consolidation_call,
)
from open_deep_research.nodes.completeness import (
    assess_sufficiency,
    assess_completeness,
    _gaploop_decision,
    _target_property_coverage,
    AbsenceJudgement,
    judge_absence,
    _make_absence_judge_call,
)


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


from open_deep_research.nodes.supervisor import (
    _lead_researcher_tools,
    supervisor,
    supervisor_tools,
    supervisor_subgraph,
)

from open_deep_research.nodes.researcher import (
    researcher,
    researcher_tools,
    compress_research,
    execute_tool_safely,
    researcher_subgraph,
)

from open_deep_research.nodes.persistence import (
    persist_research,
    _checkpoint_dossier,
    _facts_report_md,
    _merge_dossier,
)


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