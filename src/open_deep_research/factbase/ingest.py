"""Ingestion orchestration: records -> resolve/identity/registry -> conflict/promotion -> atomic write."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import aiosqlite

from . import conflict, identity, model, promotion


def _trusted_threshold(pd) -> str:
    return getattr(pd, "trust_threshold", "reputable")


class Ingestor:
    def __init__(self, conn: aiosqlite.Connection, *, profile, resolver, registry):
        self._conn = conn
        self._profile = profile
        self._resolver = resolver
        self._registry = registry

    async def ingest(self, *, run_id: int, records: list[dict]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        # Build candidate Facts (resolve + identity + registry); quarantine misses.
        candidates = []  # (rec, Fact-without-id, source_id)
        await self._conn.execute("BEGIN")
        try:
            for rec in records:
                pd = self._profile.property(rec["property"])
                instance_key = self._resolver.resolve(rec.get("instance_name", ""))
                if instance_key is None:
                    await self._conn.execute(
                        "INSERT INTO unresolved_instance (raw_name, run_id, created_at) VALUES (?,?,?)",
                        (rec.get("instance_name"), run_id, now),
                    )
                    continue
                quals = {q: rec.get("qualifiers", {}).get(q) for q in pd.identity_qualifiers}
                tk = identity.tuple_key(abs(hash(instance_key)) % (10**9), pd.name, quals)
                url = rec.get("source_url", "")
                source_id = await self._source_id(url, now)
                meets_bar = self._registry.meets_bar(url, _trusted_threshold(pd))
                # A fact is "unspecified-required" only when the property defines
                # identity qualifiers yet the record supplied none of them — i.e. the
                # model abstained on identity entirely. Supplying at least one primary
                # qualifier (the others defaulting to unspecified) is promotable.
                has_unspec = bool(pd.identity_qualifiers) and all(
                    quals.get(q) is None for q in pd.identity_qualifiers
                )
                as_of = int(rec["as_of"]) if str(rec.get("as_of", "")).isdigit() else None
                f = model.Fact(fact_id=None, tuple_key=tk, as_of=as_of, value=rec["value"],
                               unit=rec.get("unit"), source_meets_bar=meets_bar,
                               has_unspecified_required=has_unspec)
                candidates.append((rec, f, source_id))

            # Group by (tuple_key, as_of), insert facts, then detect conflicts + promote.
            buckets: dict[tuple, list] = {}
            for rec, f, sid in candidates:
                buckets.setdefault((f.tuple_key, f.as_of), []).append((rec, f, sid))

            for (tk, as_of), items in buckets.items():
                for rec, f, sid in items:
                    # dedup: same tuple/as_of/value/unit/source already present?
                    cur = await self._conn.execute(
                        "SELECT id FROM fact WHERE tuple_key=? AND IFNULL(as_of,-1)=IFNULL(?,-1) "
                        "AND value=? AND IFNULL(unit,'')=IFNULL(?,'') AND source_id=?",
                        (tk, as_of, f.value, f.unit, sid),
                    )
                    if await cur.fetchone():
                        continue
                    c = await self._conn.execute(
                        "INSERT INTO fact (tuple_key, qualifiers_json, as_of, value, unit, source_id, "
                        "admission, lifecycle, run_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (tk, json.dumps({}), as_of, f.value, f.unit, sid, "provisional", "current", run_id, now),
                    )
                    f.fact_id = c.lastrowid
                    await self._conn.execute(
                        "INSERT INTO evidence (fact_id, quoted_span, retrieved_at) VALUES (?,?,?)",
                        (f.fact_id, rec.get("evidence_span"), now),
                    )
                    await self._conn.execute(
                        "INSERT INTO fact_revision (fact_id, change, cause, why, created_at) VALUES (?,?,?,?,?)",
                        (f.fact_id, f"value={f.value}", "ingest", "new fact from run", now),
                    )

                # Conflict detection over the bucket's freshly-inserted facts (run once).
                bucket_facts = [f for _, f, _ in items if f.fact_id is not None]
                intents = conflict.detect(bucket_facts)
                for intent in intents:
                    if isinstance(intent, model.OpenConflict):
                        cc = await self._conn.execute(
                            "INSERT INTO conflict (tuple_key, as_of, status, created_at) VALUES (?,?, 'open', ?)",
                            (tk, as_of, now),
                        )
                        for fid in intent.fact_ids:
                            await self._conn.execute(
                                "INSERT INTO conflict_member (conflict_id, fact_id) VALUES (?,?)",
                                (cc.lastrowid, fid),
                            )

                has_open = any(isinstance(i, model.OpenConflict) for i in intents)
                for f in bucket_facts:
                    if isinstance(promotion.evaluate(f, bucket_facts, has_open_conflict=has_open), model.Promote):
                        await self._conn.execute(
                            "UPDATE fact SET admission='trusted' WHERE id=?", (f.fact_id,))
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    async def _source_id(self, url: str, now: str) -> int:
        cur = await self._conn.execute("SELECT id FROM source WHERE url_or_domain=?", (url,))
        row = await cur.fetchone()
        if row:
            return row[0]
        tier = self._registry.tier(url)
        c = await self._conn.execute(
            "INSERT INTO source (url_or_domain, tier, flags_json) VALUES (?,?,?)",
            (url, tier, json.dumps(self._registry.flags(url))),
        )
        return c.lastrowid
