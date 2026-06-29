"""Main LangGraph implementation for the Deep Research agent."""

import logging

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from open_deep_research.claude_agent_chat import configurable_claude_model
from open_deep_research.configuration import Configuration
from open_deep_research.nodes.brief import (
    _steer_brief_with_catalog,
    answer_from_dossier,
    assess_knowledge,
    clarify_with_user,
    write_research_brief,
)
from open_deep_research.nodes.common import (
    ALL_RESEARCH_FAILED_SENTINEL,
    COMPRESSION_FAILED_SENTINEL,
    REPORT_FAILED_PREFIX,
    _fact_fetch_text,
    _is_empty_run,
    _raw_text_source_count,
    _report_is_failed,
    _run_fact_count,
    recommended_recursion_limit,
)
from open_deep_research.nodes.completeness import (
    AbsenceJudgement,
    _gaploop_decision,
    _make_absence_judge_call,
    _target_property_coverage,
    assess_completeness,
    assess_sufficiency,
    judge_absence,
)
from open_deep_research.nodes.extraction import (
    ExtractionResult,
    FactRecord,
    _make_fact_model_call,
    _maybe_propose_extensions,
    extract_facts,
    preallocate_run,
)
from open_deep_research.nodes.persistence import (
    _checkpoint_dossier,
    _facts_report_md,
    _merge_dossier,
    persist_research,
)
from open_deep_research.nodes.profiles import (
    _effective_profile_name,
    _resolve_subject,
    resolve_target_properties,
    select_profile,
)
from open_deep_research.nodes.qualifiers import resolve_required_qualifiers
from open_deep_research.nodes.report import final_report_generation
from open_deep_research.nodes.researcher import (
    compress_research,
    execute_tool_safely,
    researcher,
    researcher_subgraph,
    researcher_tools,
)
from open_deep_research.nodes.supervisor import (
    _lead_researcher_tools,
    supervisor,
    supervisor_subgraph,
    supervisor_tools,
)
from open_deep_research.nodes.synthesis import (
    NameConsolidation,
    _best_singular_row,
    _consolidate_name_group,
    _display_value,
    _facts_answer_text,
    _make_name_consolidation_call,
    _synthesize_dossier,
    answer_from_facts,
    synthesize_narrative,
)
from open_deep_research.state import (
    AgentInputState,
    AgentState,
    ConductResearch,
    ResearchComplete,
)
from open_deep_research.utils import think_tool

logger = logging.getLogger(__name__)

# Initialize a configurable model that we will use throughout the agent.
# Backed by CLI agents (Gemini, Claude/code, or Codex). Default = gemini:gemini-2.5-flash:
# the standard Gemini CLI is reliable for structured output; Codex's exec sandbox runs
# repo commands (e.g. pytest) so it stays opt-in per role until that's restricted.
configurable_model = configurable_claude_model(
    default_config={"model": "gemini:gemini-2.5-flash"}
)

# Public surface: this assembler re-exports every node + helper it imports so that
# `langgraph.json`'s entry point and `from open_deep_research import deep_researcher as
# dr; dr.X` keep resolving after the move into nodes/. A missing re-export surfaces
# immediately as an AttributeError in the graph-identity / wiring tests.
__all__ = [
    "ALL_RESEARCH_FAILED_SENTINEL",
    "AbsenceJudgement",
    "COMPRESSION_FAILED_SENTINEL",
    "ConductResearch",
    "ExtractionResult",
    "FactRecord",
    "NameConsolidation",
    "REPORT_FAILED_PREFIX",
    "ResearchComplete",
    "_best_singular_row",
    "_checkpoint_dossier",
    "_consolidate_name_group",
    "_display_value",
    "_effective_profile_name",
    "_fact_fetch_text",
    "_facts_answer_text",
    "_facts_report_md",
    "_gaploop_decision",
    "_is_empty_run",
    "_lead_researcher_tools",
    "_make_absence_judge_call",
    "_make_fact_model_call",
    "_make_name_consolidation_call",
    "_maybe_propose_extensions",
    "_merge_dossier",
    "_raw_text_source_count",
    "_report_is_failed",
    "_resolve_subject",
    "_run_fact_count",
    "_steer_brief_with_catalog",
    "_synthesize_dossier",
    "_target_property_coverage",
    "answer_from_dossier",
    "answer_from_facts",
    "assess_completeness",
    "assess_knowledge",
    "assess_sufficiency",
    "clarify_with_user",
    "compress_research",
    "configurable_model",
    "deep_researcher",
    "deep_researcher_builder",
    "execute_tool_safely",
    "extract_facts",
    "final_report_generation",
    "judge_absence",
    "persist_research",
    "preallocate_run",
    "recommended_recursion_limit",
    "researcher",
    "researcher_subgraph",
    "researcher_tools",
    "resolve_required_qualifiers",
    "resolve_target_properties",
    "route_after_extract",
    "route_after_research",
    "select_profile",
    "supervisor",
    "supervisor_subgraph",
    "supervisor_tools",
    "synthesize_narrative",
    "think_tool",
    "write_research_brief",
]


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
deep_researcher_builder.add_node("resolve_required_qualifiers", resolve_required_qualifiers)  # Resolve inferred qualifiers for required fields
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
deep_researcher_builder.add_edge("extract_facts", "resolve_required_qualifiers")   # Facts extracted -> resolve required qualifiers
deep_researcher_builder.add_conditional_edges(                                      # Qualifiers resolved -> persist (default) | sufficiency (facts-first) | completeness (whole-profile)
    "resolve_required_qualifiers", route_after_extract,
    {"persist_research": "persist_research", "assess_sufficiency": "assess_sufficiency",
     "assess_completeness": "assess_completeness"})
deep_researcher_builder.add_edge("answer_from_facts", "persist_research")           # Facts answer -> persist
deep_researcher_builder.add_edge("synthesize_narrative", "persist_research")        # Narrative dossier -> persist
deep_researcher_builder.add_edge("persist_research", END)                          # Final exit point

# Compile the complete deep researcher workflow
deep_researcher = deep_researcher_builder.compile()