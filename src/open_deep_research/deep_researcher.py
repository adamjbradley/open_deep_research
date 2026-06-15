"""Main LangGraph implementation for the Deep Research agent."""

import asyncio
import logging
from datetime import datetime, timezone
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
    final_report_generation_prompt,
    knowledge_assessment_prompt,
    lead_researcher_prompt,
    merge_reports_prompt,
    research_system_prompt,
    subject_resolution_prompt,
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
)
from open_deep_research.storage import (
    extract_sources,
    get_db_path,
    get_subject_by_slug,
    get_subject_names,
    log_research_run,
    preallocate_run as preallocate_run_storage,
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

# Initialize a configurable model that we will use throughout the agent.
# Backed by the Claude Agent SDK (Claude Code) so all LLM activity bills against
# a Claude subscription rather than per-token API credits. See claude_agent_chat.py.
configurable_model = configurable_claude_model()

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
        "model": configurable.research_model,
        "max_tokens": configurable.research_model_max_tokens,
        "api_key": get_api_key_for_model(configurable.research_model, config),
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
                "model": configurable.research_model,
                "max_tokens": configurable.research_model_max_tokens,
                "api_key": get_api_key_for_model(configurable.research_model, config),
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
        research_model = (
            configurable_model
            .with_structured_output(ResearchQuestion)
            .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
            .with_config({
                "model": configurable.research_model,
                "max_tokens": configurable.research_model_max_tokens,
                "api_key": get_api_key_for_model(configurable.research_model, config),
                "tags": ["langsmith:nostream"],
            })
        )
        response = await research_model.ainvoke([HumanMessage(content=transform_messages_into_research_topic_prompt.format(
            messages=question, date=get_today_str()
        ))])
        research_brief = response.research_brief

    supervisor_system_prompt = lead_researcher_prompt.format(
        date=get_today_str(),
        max_concurrent_research_units=configurable.max_concurrent_research_units,
        max_researcher_iterations=configurable.max_researcher_iterations
    )
    # Static edge to research_supervisor (see graph wiring).
    return {
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
    research_model_config = {
        "model": configurable.research_model,
        "max_tokens": configurable.research_model_max_tokens,
        "api_key": get_api_key_for_model(configurable.research_model, config),
        "tags": ["langsmith:nostream"]
    }
    
    # Available tools: research delegation, completion signaling, and strategic thinking
    lead_researcher_tools = [ConductResearch, ResearchComplete, think_tool]
    
    # Configure model with tools, retry logic, and model settings
    research_model = (
        configurable_model
        .bind_tools(lead_researcher_tools)
        .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
        .with_config(research_model_config)
    )
    
    # Step 2: Generate supervisor response based on current context
    supervisor_messages = state.get("supervisor_messages", [])
    response = await research_model.ainvoke(supervisor_messages)
    
    # Step 3: Update state and proceed to tool execution
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
        reflection_content = tool_call["args"]["reflection"]
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
    
    if conduct_research_calls:
        try:
            # Limit concurrent research units to prevent resource exhaustion
            allowed_conduct_research_calls = conduct_research_calls[:configurable.max_concurrent_research_units]
            overflow_conduct_research_calls = conduct_research_calls[configurable.max_concurrent_research_units:]
            
            # Execute research tasks in parallel
            research_tasks = [
                researcher_subgraph.ainvoke({
                    "researcher_messages": [
                        HumanMessage(content=tool_call["args"]["research_topic"])
                    ],
                    "research_topic": tool_call["args"]["research_topic"]
                }, config) 
                for tool_call in allowed_conduct_research_calls
            ]
            
            tool_results = await asyncio.gather(*research_tasks)
            
            # Create tool messages with research results
            for observation, tool_call in zip(tool_results, allowed_conduct_research_calls):
                all_tool_messages.append(ToolMessage(
                    content=observation.get("compressed_research", "Error synthesizing research report: Maximum retries exceeded"),
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
            
            # Aggregate raw notes from all research results
            raw_notes_concat = "\n".join([
                "\n".join(observation.get("raw_notes", [])) 
                for observation in tool_results
            ])
            
            if raw_notes_concat:
                update_payload["raw_notes"] = [raw_notes_concat]
                
        except Exception as e:
            # Handle research execution errors
            if is_token_limit_exceeded(e, configurable.research_model) or True:
                # Token limit exceeded or other error - end research phase
                return Command(
                    goto=END,
                    update={
                        "notes": get_notes_from_tool_calls(supervisor_messages),
                        "research_brief": state.get("research_brief", "")
                    }
                )
    
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
    research_model_config = {
        "model": configurable.research_model,
        "max_tokens": configurable.research_model_max_tokens,
        "api_key": get_api_key_for_model(configurable.research_model, config),
        "tags": ["langsmith:nostream"]
    }
    
    # Prepare system prompt with MCP context if available
    researcher_prompt = research_system_prompt.format(
        mcp_prompt=configurable.mcp_prompt or "", 
        date=get_today_str()
    )
    
    # Configure model with tools, retry logic, and settings
    research_model = (
        configurable_model
        .bind_tools(tools)
        .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
        .with_config(research_model_config)
    )
    
    # Step 3: Generate researcher response with system context
    messages = [SystemMessage(content=researcher_prompt)] + researcher_messages
    response = await research_model.ainvoke(messages)
    
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
    """Safely execute a tool with error handling."""
    try:
        return await tool.ainvoke(args, config)
    except Exception as e:
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
    
    # Execute all tool calls in parallel
    tool_calls = most_recent_message.tool_calls
    tool_execution_tasks = [
        execute_tool_safely(tools_by_name[tool_call["name"]], tool_call["args"], config) 
        for tool_call in tool_calls
    ]
    observations = await asyncio.gather(*tool_execution_tasks)
    
    # Create tool messages from execution results
    tool_outputs = [
        ToolMessage(
            content=observation,
            name=tool_call["name"],
            tool_call_id=tool_call["id"]
        ) 
        for observation, tool_call in zip(observations, tool_calls)
    ]
    
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
            if is_token_limit_exceeded(e, configurable.research_model):
                researcher_messages = remove_up_to_last_ai_message(researcher_messages)
                continue
            
            # For other errors, continue retrying
            continue
    
    # Step 4: Return error result if all attempts failed
    raw_notes_content = "\n".join([
        str(message.content) 
        for message in filter_messages(researcher_messages, include_types=["tool", "ai"])
    ])
    
    return {
        "compressed_research": "Error synthesizing research report: Maximum retries exceeded",
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
                return {
                    "final_report": f"Error generating final report: {e}",
                    "messages": [AIMessage(content="Report generation failed due to an error")],
                    **cleared_state
                }
    
    # Step 4: Return failure result if all retries exhausted
    return {
        "final_report": "Error generating final report: Maximum retries exceeded",
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
        # Persistence is best-effort: never fail a completed run on a DB error.
        logger.warning("Failed to persist research result: %s", e)
        return {}


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


def _make_fact_model_call(configurable, config):
    """Build an async model_call(source_text, prof) -> list[dict] for the extractor.

    Mirrors the structured-output invocation pattern used elsewhere in this graph
    (singleton ``configurable_model`` -> ``with_structured_output`` -> ``with_config``
    -> ``ainvoke``). Best-effort: returns [] on any error so extraction never fails
    a completed run.
    """
    async def model_call(source_text, prof):
        try:
            prop_names = [pd.name for pd in prof.properties]
            prompt = (
                "Extract Digital-Identity facts about a COUNTRY from the source text below. "
                f"Only use these property names: {prop_names}. "
                "Emit a qualifier ONLY if the source explicitly states it; otherwise omit it (do not guess). "
                "evidence_span MUST be a verbatim substring of the source text supporting the value. "
                "If nothing is stated, return an empty list.\n\nSOURCE:\n" + (source_text or "")[:8000]
            )
            model = (
                configurable_model
                .with_structured_output(ExtractionResult)
                .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
                .with_config({
                    "model": configurable.research_model,
                    "max_tokens": configurable.research_model_max_tokens,
                    "api_key": get_api_key_for_model(configurable.research_model, config),
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
    try:
        run_id = await preallocate_run_storage(get_db_path(config), str(thread_id))
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
        return {}
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
        prof = fbprofile.load("country_digital_identity")
        reg = fbregistry.SourceRegistry.load("di_source_registry")
        model_call = _make_fact_model_call(configurable, config)
        run_id = state.get("prealloc_run_id")
        async with aiosqlite.connect(get_db_path(config)) as conn:
            await fbmig.apply(conn, fbschema.STEPS)
            sources = await fbstore.RunSourceStore(conn).read(str(thread_id))
            if run_id and any(s["capture_status"] != "raw_text" for s in sources):
                from open_deep_research.storage import set_coverage_incomplete
                await set_coverage_incomplete(get_db_path(config), run_id, True)
            all_records = []
            for s in sources:
                if s["capture_status"] != "raw_text" or not s["text"]:
                    continue
                recs = await fbextractor.extract(s["text"], prof, model_call)
                for r in recs:
                    r.setdefault("source_url", s["source_url"])
                all_records.extend(recs)
            if all_records and run_id:
                await fbingest.Ingestor(
                    conn,
                    profile=prof,
                    resolver=fbentities.CountryResolver(),
                    registry=reg,
                ).ingest(run_id=run_id, records=all_records)
    except Exception as e:
        logger.warning("extract_facts failed (non-fatal): %s", e)
    return {}


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
deep_researcher_builder.add_node("persist_research", persist_research)             # Persist results to SQLite

# Define main workflow edges. assess_knowledge (entry) branches via Command(goto)
# to answer_from_dossier / write_research_brief / clarify_with_user.
deep_researcher_builder.add_edge(START, "preallocate_run")                          # Entry point: preallocate the run id
deep_researcher_builder.add_edge("preallocate_run", "assess_knowledge")             # Then check the knowledge base
deep_researcher_builder.add_edge("answer_from_dossier", "persist_research")         # Cached answer -> log the run
deep_researcher_builder.add_edge("write_research_brief", "research_supervisor")     # Brief to research
deep_researcher_builder.add_edge("research_supervisor", "final_report_generation") # Research to report
deep_researcher_builder.add_edge("final_report_generation", "extract_facts")       # Report to fact extraction
deep_researcher_builder.add_edge("extract_facts", "persist_research")              # Fact extraction to persistence
deep_researcher_builder.add_edge("persist_research", END)                          # Final exit point

# Compile the complete deep researcher workflow
deep_researcher = deep_researcher_builder.compile()