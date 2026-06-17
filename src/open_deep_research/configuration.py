"""Configuration management for the Open Deep Research system."""

import os
from enum import Enum
from typing import Any, List, Optional

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field


class SearchAPI(Enum):
    """Enumeration of available search API providers."""

    CLAUDE = "claude"
    GEMINI = "gemini"
    CODEX = "codex"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    TAVILY = "tavily"
    NONE = "none"

class MCPConfig(BaseModel):
    """Configuration for Model Context Protocol (MCP) servers."""
    
    url: Optional[str] = Field(
        default=None,
        optional=True,
    )
    """The URL of the MCP server"""
    tools: Optional[List[str]] = Field(
        default=None,
        optional=True,
    )
    """The tools to make available to the LLM"""
    auth_required: Optional[bool] = Field(
        default=False,
        optional=True,
    )
    """Whether the MCP server requires authentication"""

class Configuration(BaseModel):
    """Main configuration class for the Deep Research agent."""
    
    # General Configuration
    max_structured_output_retries: int = Field(
        default=3,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 3,
                "min": 1,
                "max": 10,
                "description": "Maximum number of retries for structured output calls from models"
            }
        }
    )
    allow_clarification: bool = Field(
        default=True,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": True,
                "description": "Whether to allow the researcher to ask the user clarifying questions before starting research"
            }
        }
    )
    max_concurrent_research_units: int = Field(
        default=5,
        metadata={
            "x_oap_ui_config": {
                "type": "slider",
                "default": 5,
                "min": 1,
                "max": 20,
                "step": 1,
                "description": "Maximum number of research units to run concurrently. This will allow the researcher to use multiple sub-agents to conduct research. Note: with more concurrency, you may run into rate limits."
            }
        }
    )
    # Research Configuration
    search_api: SearchAPI = Field(
        default=SearchAPI.TAVILY,
        metadata={
            "x_oap_ui_config": {
                "type": "select",
                "default": "tavily",
                "description": "Search API to use for research. NOTE: Make sure your Researcher Model supports the selected search API.",
                "options": [
                    {"label": "Claude Code Web Search", "value": SearchAPI.CLAUDE.value},
                    {"label": "Gemini Google Search", "value": SearchAPI.GEMINI.value},
                    {"label": "Codex Web Search", "value": SearchAPI.CODEX.value},
                    {"label": "Tavily", "value": SearchAPI.TAVILY.value},
                    {"label": "OpenAI Native Web Search", "value": SearchAPI.OPENAI.value},
                    {"label": "Anthropic Native Web Search", "value": SearchAPI.ANTHROPIC.value},
                    {"label": "None", "value": SearchAPI.NONE.value}
                ]
            }
        }
    )
    max_researcher_iterations: int = Field(
        default=6,
        metadata={
            "x_oap_ui_config": {
                "type": "slider",
                "default": 6,
                "min": 1,
                "max": 10,
                "step": 1,
                "description": "Maximum number of research iterations for the Research Supervisor. This is the number of times the Research Supervisor will reflect on the research and ask follow-up questions."
            }
        }
    )
    max_react_tool_calls: int = Field(
        default=10,
        metadata={
            "x_oap_ui_config": {
                "type": "slider",
                "default": 10,
                "min": 1,
                "max": 30,
                "step": 1,
                "description": "Maximum number of tool calling iterations to make in a single researcher step."
            }
        }
    )
    # Model Configuration
    summarization_model: str = Field(
        default="gemini:gemini-2.5-flash",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "gemini:gemini-2.5-flash",
                "description": "Model for summarizing research results from Tavily search results. With the Claude Agent SDK backend, use a family ('haiku'/'sonnet'/'opus') or a full 'claude-*' id."
            }
        }
    )
    summarization_model_max_tokens: int = Field(
        default=8192,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 8192,
                "description": "Maximum output tokens for summarization model"
            }
        }
    )
    max_content_length: int = Field(
        default=50000,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 50000,
                "min": 1000,
                "max": 200000,
                "description": "Maximum character length for webpage content before summarization"
            }
        }
    )
    supervisor_model: str = Field(
        default="gemini:gemini-2.5-flash",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "gemini:gemini-2.5-flash",
                "description": "Model for the Research Supervisor (planning and strategy). Backend is chosen per role by an optional provider prefix: 'claude:opus' (Claude Code), 'gemini:2.5-pro' (Gemini CLI), 'codex:gpt-5' (Codex CLI). No prefix = Claude family."
            }
        }
    )
    researcher_model: str = Field(
        default="gemini:gemini-2.5-flash",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "gemini:gemini-2.5-flash",
                "description": "Model for individual Researchers (tool execution). Backend is chosen per role by an optional provider prefix: 'claude:opus' (Claude Code), 'gemini:2.5-pro' (Gemini CLI), 'codex:gpt-5' (Codex CLI). No prefix = Claude family."
            }
        }
    )
    researcher_model_max_tokens: int = Field(
        default=10000,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 10000,
                "description": "Maximum output tokens for research model"
            }
        }
    )
    compression_model: str = Field(
        default="gemini:gemini-2.5-flash",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "gemini:gemini-2.5-flash",
                "description": "Model for compressing research findings from sub-agents (Claude Agent SDK family or 'claude-*' id)."
            }
        }
    )
    compression_model_max_tokens: int = Field(
        default=8192,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 8192,
                "description": "Maximum output tokens for compression model"
            }
        }
    )
    final_report_model: str = Field(
        default="gemini:gemini-2.5-flash",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "gemini:gemini-2.5-flash",
                "description": "Model for writing the final report from all research findings (Claude Agent SDK family or 'claude-*' id)."
            }
        }
    )
    final_report_model_max_tokens: int = Field(
        default=10000,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 10000,
                "description": "Maximum output tokens for final report model"
            }
        }
    )
    # Result persistence
    persist_results: bool = Field(
        default=True,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": True,
                "description": "Whether to automatically store each completed research run (report, sources, raw notes, config) in the local SQLite database."
            }
        }
    )
    database_path: str = Field(
        default="research_results.db",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "research_results.db",
                "description": "Path to the local SQLite database file used to store research results. Can also be set via the RESEARCH_DB_PATH environment variable."
            }
        }
    )
    run_staleness_minutes: int = Field(
        default=60,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 60,
                "min": 5,
                "description": "Age (minutes) after which a still-'running' research_runs row is treated as abandoned (e.g. a crashed/killed run) and reaped to status='error' at the start of the next run. Should comfortably exceed a normal run's wall-clock so live runs are never reaped."
            }
        }
    )
    accumulate_by_subject: bool = Field(
        default=True,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": True,
                "description": "Group research runs by canonical subject and merge each new report into that subject's accumulated dossier (preserving prior findings and adding new ones). When off, each run is stored independently with no merging (no extra LLM calls)."
            }
        }
    )
    normalize_fact_values: bool = Field(
        default=True,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": True,
                "description": "Collapse semantically-equal fact values that differ only in surface form (e.g. 'Aadhaar' / 'Aadhaar Card', 'Aadhaar Act' / 'Aadhaar Act, 2016', '~99' / '99%') onto one canonical value for dedup and conflict detection. Kill-switch: turn off if it over-merges genuinely-distinct values."
            }
        }
    )
    facts_first_mode: bool = Field(
        default=False,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": False,
                "description": "Gather structured facts sufficient to answer the question and answer directly from the fact base, SKIPPING the prose final report. Targets only the profile properties the question needs, loops to research gaps until sufficient (see max_fact_rounds), then answers from the facts. When off, the normal report-writing flow runs."
            }
        }
    )
    profile_name: str = Field(
        default="country_digital_identity",
        metadata={"x_oap_ui_config": {
            "type": "text",
            "default": "country_digital_identity",
            "description": "Name of the factbase domain profile (YAML file stem under factbase/profiles/) used for fact extraction.",
        }},
    )
    registry_name: str = Field(
        default="di_source_registry",
        metadata={"x_oap_ui_config": {
            "type": "text",
            "default": "di_source_registry",
            "description": "Name of the factbase source registry (YAML file stem under factbase/profiles/) used for source-trust tiers.",
        }},
    )
    compile_extraction_prompt: bool = Field(
        default=True,
        metadata={"x_oap_ui_config": {
            "type": "boolean",
            "default": True,
            "description": "Compile the fact-extraction prompt from the profile (property kinds, descriptions, enum vocabularies, qualifiers). When false, fall back to the names-only baseline.",
        }},
    )
    max_fact_rounds: int = Field(
        default=2,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 2,
                "min": 1,
                "max": 5,
                "description": "Facts-first mode only: max research rounds. After each round, if a target property still has no fact, re-research the gaps (up to this many rounds). 1 = single pass (no gap loop). Each extra round only extracts newly-fetched sources."
            }
        }
    )
    facts_answer_polish_model: Optional[str] = Field(
        default=None,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "",
                "description": "Facts-first mode only: optional cheap model to polish the deterministic facts answer into prose (grounded only in the facts). Empty = use the summarization_model. The deterministic answer is always produced; polish is best-effort."
            }
        }
    )
    use_knowledge_base: bool = Field(
        default=True,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": True,
                "description": "Before researching, match the question to an existing subject and assess whether the stored dossier already answers it. The research is then scoped to verify existing knowledge or to research the whole subject when information is missing, and results are merged back. When off, every question is researched from scratch."
            }
        }
    )
    # MCP server configuration
    mcp_config: Optional[MCPConfig] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "mcp",
                "description": "MCP server configuration"
            }
        }
    )
    mcp_prompt: Optional[str] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "description": "Any additional instructions to pass along to the Agent regarding the MCP tools that are available to it."
            }
        }
    )


    @classmethod
    def from_runnable_config(
        cls, config: Optional[RunnableConfig] = None
    ) -> "Configuration":
        """Create a Configuration instance from a RunnableConfig."""
        configurable = config.get("configurable", {}) if config else {}
        field_names = list(cls.model_fields.keys())
        values: dict[str, Any] = {
            field_name: os.environ.get(field_name.upper(), configurable.get(field_name))
            for field_name in field_names
        }
        return cls(**{k: v for k, v in values.items() if v is not None})

    class Config:
        """Pydantic configuration."""
        
        arbitrary_types_allowed = True