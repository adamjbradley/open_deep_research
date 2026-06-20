"""Fact-base extraction node helpers: models, model-call factory, and graph nodes."""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from open_deep_research.claude_agent_chat import configurable_claude_model
from open_deep_research.configuration import Configuration
from open_deep_research.failover import new_run_tracker
from open_deep_research.state import AgentState
from open_deep_research.storage import (
    get_db_path,
    preallocate_run as preallocate_run_storage,
    reap_stale_running,
)
from open_deep_research.utils import get_api_key_for_model
from open_deep_research.nodes.common import _fact_fetch_text
from open_deep_research.nodes.profiles import _effective_profile_name

logger = logging.getLogger(__name__)

# Configurable model shared by all extraction helpers (same default as deep_researcher.py).
configurable_model = configurable_claude_model(
    default_config={"model": "gemini:gemini-2.5-flash"}
)


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
