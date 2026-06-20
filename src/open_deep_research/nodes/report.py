"""Final report generation node for the Deep Research agent."""

import logging

from langchain_core.messages import AIMessage, HumanMessage, get_buffer_string
from langchain_core.runnables import RunnableConfig

from open_deep_research.claude_agent_chat import configurable_claude_model
from open_deep_research.configuration import Configuration
from open_deep_research.nodes.common import (
    ALL_RESEARCH_FAILED_SENTINEL,
    REPORT_FAILED_PREFIX,
)
from open_deep_research.prompts import final_report_generation_prompt
from open_deep_research.state import AgentState
from open_deep_research.utils import (
    get_api_key_for_model,
    get_model_token_limit,
    get_today_str,
    is_token_limit_exceeded,
)

logger = logging.getLogger(__name__)

configurable_model = configurable_claude_model(
    default_config={"model": "gemini:gemini-2.5-flash"}
)


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

    # If the research phase wholesale-failed (every unit exhausted its model chain),
    # the supervisor emits ALL_RESEARCH_FAILED_SENTINEL into raw_notes. Surface that as
    # a failed report (caught by _report_is_failed) instead of letting the writer
    # synthesize a misleading report from per-unit error notes — and skip the writer call.
    raw_notes = state.get("raw_notes", [])
    if any(ALL_RESEARCH_FAILED_SENTINEL in rn for rn in raw_notes):
        logger.error("All research units failed; emitting failed report without a writer call")
        return {
            "final_report": f"{REPORT_FAILED_PREFIX} all research units failed (no usable findings)",
            "messages": [AIMessage(content="Report generation skipped: all research units failed")],
            **cleared_state,
        }

    # Step 2: Configure the final report generation model
    configurable = Configuration.from_runnable_config(config)
    writer_model_config = {
        "model": configurable.final_report_model,
        "model_chain": configurable.model_chain("final_report"),
        "stage": "final_report",
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
