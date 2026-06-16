"""Load country population directly into the factbase from vendored World Bank data.

Reads data/population.yaml (or an injected `data` dict for tests), builds one record per
country, and feeds the existing Ingestor so the facts get canonicalization, source-tiering
(World Bank = authoritative), conflict handling, and promotion to 'trusted'. No LLM/graph.
"""
from __future__ import annotations

import aiosqlite

from open_deep_research import storage as _storage
from . import migrations as _mig, schema as _schema
from .entities import CountryResolver
from .ingest import Ingestor
from .profile import load as load_profile
from .registry import SourceRegistry

_SOURCE_URL = "https://data.worldbank.org/indicator/SP.POP.TOTL"
_EVIDENCE = "World Bank SP.POP.TOTL most-recent estimate"


def _load_data() -> dict:
    import yaml
    from importlib.resources import files

    try:
        text = files("open_deep_research.factbase.data").joinpath("population.yaml").read_text(
            encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            "population.yaml missing — run: uv run python scripts/gen_population.py"
        ) from exc
    return yaml.safe_load(text) or {}


async def load_population(db_path: str, *, profile_name: str = "country_population",
                          registry_name: str = "country_population_source_registry",
                          data: dict | None = None) -> dict:
    """Ingest one most-recent population fact per country. Returns counts + skipped codes."""
    data = _load_data() if data is None else data
    prof = load_profile(profile_name)
    reg = SourceRegistry.load(registry_name)
    resolver = CountryResolver()

    records, skipped = [], []
    for alpha3, entry in sorted(data.items()):
        name = resolver.instance_name(alpha3)
        if resolver.resolve(name) is None:        # unknown/aggregate code -> report, don't drop
            skipped.append(alpha3)
            continue
        records.append({
            "property": "population", "instance_name": name,
            "value": str(entry["value"]), "as_of": entry["year"],
            "source_url": _SOURCE_URL, "evidence_span": _EVIDENCE,
        })

    run_id = await _storage.preallocate_run(db_path, "population-load")
    async with aiosqlite.connect(db_path) as conn:
        await _storage._ensure_schema(conn)
        await _mig.apply(conn, _schema.STEPS)
        await Ingestor(conn, profile=prof, resolver=resolver, registry=reg).ingest(
            run_id=run_id, records=records)
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT COUNT(*) n, SUM(admission='trusted') t, COUNT(DISTINCT instance_key) k "
            "FROM fact WHERE property_name='population' AND run_id=?", (str(run_id),))  # run_id col is TEXT
        r = await cur.fetchone()
    return {"loaded": r["n"] or 0, "trusted": r["t"] or 0,
            "instances": r["k"] or 0, "skipped": skipped}
