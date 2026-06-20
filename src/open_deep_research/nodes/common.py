"""Shared utility functions and sentinel constants used across node modules."""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Failure sentinels emitted by compress_research / final_report_generation when their
# model calls exhaust retries. They are real strings that flow into notes / the report,
# so persistence must detect them (see _report_is_failed) and avoid saving them as a
# "completed" run or merging them into the subject dossier (which would poison the KB).
COMPRESSION_FAILED_SENTINEL = "Error synthesizing research report: Maximum retries exceeded"
ALL_RESEARCH_FAILED_SENTINEL = (
    "Error: all research units failed (no usable findings). "
    "Likely all model backends are unavailable (quota/auth). See run failovers."
)
REPORT_FAILED_PREFIX = "Error generating final report:"


def _report_is_failed(report: Optional[str]) -> bool:
    """Whether a final report is empty or a generation-failure sentinel (not real content)."""
    if not report or not report.strip():
        return True
    stripped = report.strip()
    return (stripped.startswith(REPORT_FAILED_PREFIX)
            or stripped == COMPRESSION_FAILED_SENTINEL
            or stripped == ALL_RESEARCH_FAILED_SENTINEL)


def _is_empty_run(*, fact_count: int, raw_text_source_count: int) -> bool:
    """A run that gathered nothing: 0 researched facts AND 0 raw_text sources captured.

    Distinct from a 'thin' run (sources gathered, few facts) -- that may be a legitimately
    sparse country and is surfaced, not failed.
    """
    return fact_count == 0 and raw_text_source_count == 0


async def _run_fact_count(db_path: str, run_id) -> int:
    import aiosqlite
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM fact WHERE run_id=?", (run_id,))
        return int((await cur.fetchone())[0])


async def _raw_text_source_count(db_path: str, thread_id: str) -> int:
    import aiosqlite
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM run_source WHERE thread_id=? AND capture_status='raw_text'",
            (thread_id,))
        return int((await cur.fetchone())[0])


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
