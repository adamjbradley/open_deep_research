"""Claude-as-judge for prose stages: a fixed rubric, structured 0-10 scores.

The judge is the local Claude (subscription) -- it is NOT one of the compared models, just
the scorer. The same rubric runs on every prose cell so scores are comparable. Best-effort:
a judge failure returns ``None`` (the cell still reports validity + latency), never aborts.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field


class JudgeScore(BaseModel):
    """A prose output scored on three axes (0-10 each)."""

    grounding: int = Field(description="0-10: claims supported by the provided source/findings; no fabrication.")
    relevance: int = Field(description="0-10: addresses the stage's actual task.")
    coherence: int = Field(description="0-10: clear, well-structured, readable.")
    reason: str = Field(default="", description="One sentence justifying the scores.")


_JUDGE_PROMPT = """You are a strict evaluator for a research pipeline. Score the OUTPUT a \
model produced for the "{stage}" stage, given the TASK it was asked to do. Judge only what \
is shown. Penalise fabrication (claims not supported by the task's source/findings), \
off-task content, and incoherence. Return integer 0-10 scores.

TASK:
{task}

OUTPUT:
{output}
"""

# Trim very long inputs so the judge call stays cheap/bounded.
_CAP = 4000


def _mean(score: JudgeScore) -> float:
    return round((score.grounding + score.relevance + score.coherence) / 3, 2)


async def judge_prose(
    stage: str,
    task_text: str,
    output_text: str,
    *,
    build_model: Callable[..., Any],
    judge_model: str = "claude:haiku",
    max_tokens: int = 512,
) -> Optional[dict]:
    """Score one prose output. Returns a dict (score 0-10 + axes + reason) or None on failure."""
    try:
        model = build_model(judge_model, max_tokens=max_tokens).with_structured_output(JudgeScore)
        prompt = _JUDGE_PROMPT.format(
            stage=stage, task=task_text[:_CAP], output=output_text[:_CAP])
        result = await model.ainvoke([HumanMessage(content=prompt)])
        if not isinstance(result, JudgeScore):
            return None
        return {
            "score": _mean(result),
            "grounding": result.grounding,
            "relevance": result.relevance,
            "coherence": result.coherence,
            "reason": result.reason,
        }
    except Exception:  # noqa: BLE001 - judging is best-effort; never abort a benchmark run
        return None
