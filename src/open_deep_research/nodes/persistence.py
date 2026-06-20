"""Persistence nodes: save / checkpoint research runs and subject dossiers."""

import logging
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, get_buffer_string
from langchain_core.runnables import RunnableConfig

from open_deep_research.claude_agent_chat import configurable_claude_model
from open_deep_research.configuration import Configuration
from open_deep_research.failover import discard_tracker, get_tracker
from open_deep_research.prompts import merge_reports_prompt
from open_deep_research.state import AgentState
from open_deep_research.storage import (
    extract_sources,
    get_db_path,
    get_subject_by_slug,
    get_subject_names,
    log_research_run,
    save_run_and_upsert_subject,
    slugify,
)
from open_deep_research.utils import get_api_key_for_model, get_today_str
from open_deep_research.nodes.common import (
    _report_is_failed,
    _is_empty_run,
    _run_fact_count,
    _raw_text_source_count,
)
from open_deep_research.nodes.profiles import _resolve_subject

logger = logging.getLogger(__name__)

# Initialize a configurable model used by _merge_dossier.
configurable_model = configurable_claude_model(
    default_config={"model": "gemini:gemini-2.5-flash"}
)


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
