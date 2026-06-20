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

from open_deep_research.nodes.brief import (
    clarify_with_user,
    assess_knowledge,
    answer_from_dossier,
    write_research_brief,
    _steer_brief_with_catalog,
)

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