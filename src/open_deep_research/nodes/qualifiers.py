# src/open_deep_research/nodes/qualifiers.py
"""Post-extraction node: resolve facts stuck as missing a REQUIRED qualifier.

For each run fact with a value but an absent required qualifier, resolve that axis from the
fact's own evidence span (stated, or inferred only after research was attempted). Best-effort:
errors leave facts unchanged. See spec 2026-06-26-required-qualifier-resolution-design.md.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import aiosqlite
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from open_deep_research.configuration import Configuration
from open_deep_research.nodes.extraction import configurable_model
from open_deep_research.nodes.profiles import _effective_profile_name
from open_deep_research.state import AgentState
from open_deep_research.storage import get_db_path
from open_deep_research.utils import get_api_key_for_model

logger = logging.getLogger(__name__)

_INFERRED_CONFIDENCE = 0.5  # recorded for a future precedence project; inert in v1


def _make_qualifier_model_call(configurable, config):
    """Async model_call(prompt) -> raw text for resolve_qualifier (routable to a strong model)."""
    async def model_call(prompt: str) -> str:
        model = (
            configurable_model
            .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
            .with_config({
                "model": configurable.model_for("extract_facts", "researcher"),
                "model_chain": configurable.model_chain("researcher", "extract_facts"),
                "stage": "extract_facts",
                "max_tokens": configurable.researcher_model_max_tokens,
                "api_key": get_api_key_for_model(configurable.researcher_model, config),
                "tags": ["langsmith:nostream"],
            })
        )
        resp = await model.ainvoke([HumanMessage(content=prompt)])
        return str(getattr(resp, "content", "") or "")
    return model_call


async def resolve_required_qualifiers(state: AgentState, config: RunnableConfig) -> dict:
    """Resolve missing required qualifiers for run facts from their evidence spans."""
    configurable = Configuration.from_runnable_config(config)
    if not configurable.persist_results:
        return {}
    run_id = state.get("prealloc_run_id")
    if not run_id:
        return {}
    from open_deep_research.factbase import migrations, qualifier_resolve, schema
    from open_deep_research.factbase import profile as fbprofile
    try:
        prof = fbprofile.load(_effective_profile_name(state, configurable))
    except Exception as e:  # noqa: BLE001
        logger.warning("qualifier resolver: profile load failed (non-fatal): %s", e)
        return {}
    attempted = set(state.get("qualifier_research_attempted") or [])
    model_call = _make_qualifier_model_call(configurable, config)
    cap = configurable.max_qualifier_resolutions
    counts = {"stated": 0, "inferred": 0, "null": 0}
    calls = 0

    async with aiosqlite.connect(get_db_path(config)) as conn:
        await migrations.apply(conn, schema.STEPS)
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT f.id, f.property_name, f.instance_key, f.value, f.qualifiers_json, "
            "e.quoted_span FROM fact f LEFT JOIN evidence e ON e.fact_id = f.id "
            "WHERE f.run_id = ? AND f.soft_deleted_at IS NULL", (str(run_id),))
        rows = await cur.fetchall()
        cap_hit = False
        for row in rows:
            if cap_hit:
                break
            try:
                pd = prof.property(row["property_name"])
            except KeyError:
                continue
            req = list(getattr(pd, "required_qualifiers", []) or [])
            if not req:
                continue
            quals = json.loads(row["qualifiers_json"] or "{}")
            span = row["quoted_span"]
            if not span:
                continue
            enums = getattr(pd, "qualifier_enums", {}) or {}
            for q in req:
                if quals.get(q):
                    continue  # already present
                if calls >= cap:
                    logger.info("qualifier resolver hit cap (%d); remaining facts route to research", cap)
                    cap_hit = True
                    break
                allow = f"{row['property_name']}::{q}" in attempted
                calls += 1
                try:
                    res = await qualifier_resolve.resolve_qualifier(
                        value=row["value"], instance_name=row["instance_key"],
                        property_name=row["property_name"], qualifier=q,
                        enum=list(enums.get(q, [])), evidence_span=span,
                        allow_inference=allow, model_call=model_call)
                except Exception as e:  # noqa: BLE001
                    logger.warning("qualifier resolver call failed (non-fatal): %s", e)
                    res = None
                if not res:
                    counts["null"] += 1
                    continue
                counts[res["basis"]] += 1
                quals[q] = res["value"]
                inferred = res["basis"] == "inferred"
                prov = json.loads(row["qualifier_provenance_json"] or "{}") if "qualifier_provenance_json" in row.keys() else {}
                if inferred:
                    prov[q] = "inferred"
                await conn.execute(
                    "UPDATE fact SET qualifiers_json=?, qualifier_provenance_json=?, "
                    "confidence=COALESCE(?, confidence) WHERE id=?",
                    (json.dumps(quals), json.dumps(prov) if prov else None,
                     _INFERRED_CONFIDENCE if inferred else None, row["id"]))
                await conn.execute(
                    "INSERT INTO fact_revision (fact_id, change, cause, why, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (row["id"], f"{q}={res['value']} ({res['basis']})",
                     "qualifier_resolve", "required qualifier resolved",
                     datetime.now(timezone.utc).isoformat()))
        await conn.commit()
    logger.info("qualifier resolver: stated=%(stated)d inferred=%(inferred)d null=%(null)d", counts)
    return {}
