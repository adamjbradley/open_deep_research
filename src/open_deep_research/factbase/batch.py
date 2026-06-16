"""Bounded-concurrent, resumable batch runner over per-country research.

Resolves each name to an instance_key (unresolved names are REPORTED, never silently
dropped), records a ledger row per country, then runs `run_one` for each not-yet-done
item K at a time. `run_one(country_name, instance_key, *, profile_name, db_path)` is
injected so this is testable without the graph; the production default invokes
deep_researcher (see `default_run_one`).
"""
from __future__ import annotations

import asyncio

import aiosqlite

from open_deep_research import storage as _storage
from . import migrations as _mig, schema as _schema
from .batch_ledger import BatchLedger, batch_id_for
from .entities import CountryResolver


class BatchRunner:
    """Run a profile across many countries, bounded-concurrent and resumable."""

    def __init__(self, *, profile_name, db_path, concurrency=3, run_one,
                 profile_hash="", list_spec=""):
        self._profile = profile_name
        self._db = db_path
        self._k = max(1, int(concurrency))
        self._run_one = run_one
        self._profile_hash = profile_hash
        self._list_spec = list_spec
        self._resolver = CountryResolver()

    async def run(self, country_names: list[str]) -> dict:
        """Resolve names, then run each not-done country K-at-a-time. Returns a summary dict."""
        resolved: list[tuple[str, str]] = []   # (name, instance_key)
        unresolved: list[str] = []
        for name in country_names:
            key = self._resolver.resolve(name)
            if key:
                resolved.append((name, key))
            else:
                unresolved.append(name)

        # Key the batch on the RESOLVED instance keys so unresolved names don't change identity.
        spec_for_id = self._list_spec or ",".join(sorted(k for _, k in resolved))
        batch_id = batch_id_for(self._profile, spec_for_id)

        async with aiosqlite.connect(self._db) as conn:
            await _storage._ensure_schema(conn)
            await _mig.apply(conn, _schema.STEPS)
            led = BatchLedger(conn, batch_id, profile_name=self._profile,
                              profile_hash=self._profile_hash, list_spec=spec_for_id)
            await led.ensure_run()
            for name, key in resolved:
                await led.upsert_item(key, name, status="pending")
            todo = await led.pending_items(include_failed=True)

            sem = asyncio.Semaphore(self._k)

            async def worker(item):
                key, name = item["instance_key"], item["country_name"]
                async with sem:
                    await led.mark(key, status="running")
                    try:
                        run_id = await self._run_one(
                            name, key, profile_name=self._profile, db_path=self._db)
                        await led.mark(key, status="done", run_id=str(run_id))
                    except Exception as e:  # noqa: BLE001 - isolate per-country failure
                        await led.mark(key, status="failed", error=str(e))

            await asyncio.gather(*(worker(i) for i in todo))
            summary = await led.summary()
        return {"batch_id": batch_id, "summary": summary, "unresolved": unresolved,
                "resolved": [k for _, k in resolved]}


async def default_run_one(country_name, instance_key, *, profile_name, db_path) -> str:
    """Production run_one: one deep_researcher invocation scoped to a country + profile."""
    import uuid

    from langchain_core.messages import HumanMessage

    from open_deep_research.deep_researcher import deep_researcher, recommended_recursion_limit

    configurable = {
        "thread_id": str(uuid.uuid4()),
        "profile_name": profile_name,
        "database_path": db_path,
        "use_knowledge_base": False,        # fresh research per country
        "allow_clarification": False,
        "max_concurrent_research_units": 2,
        "max_researcher_iterations": 2,
    }
    topic = (f"Research {country_name} for the '{profile_name}' profile: cover its properties "
             f"with sources and dates.")
    result = await deep_researcher.ainvoke(
        {"messages": [HumanMessage(content=topic)]},
        config={"configurable": configurable,
                "recursion_limit": recommended_recursion_limit(2, 2)})
    return str(result.get("report_id") or "")
