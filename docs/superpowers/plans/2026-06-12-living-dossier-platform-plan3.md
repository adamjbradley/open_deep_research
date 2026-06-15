# Living Fact Base — Implementation Plan (Plan 3 of N: The `dossier` Read Surface)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Let a researcher *see* the facts a run produced — a read-only `dossier show <country>` (per-country fact table) and `dossier compare <property>` (cross-country table), with CSV/MD export, through one canonical render path that never presents provisional/contested facts as established.

**Architecture:** Per Architecture v6 §8 + §11 step 7. Read-only, no graph changes. **Prerequisite fix (Task 1):** the current `Ingestor` writes `fact` rows with an opaque `tuple_key` but does NOT store the instance, property, or real qualifiers — so facts can't be queried by country/property. Task 1 adds `fact.instance_key` + `fact.property_name` columns (migration v3) and populates them (+ real `qualifiers_json`). Then `FactQuery` reads them, a pure renderer formats them, and a `dossier` CLI module wires it up.

**Tech Stack:** Python 3.11, `aiosqlite`, stdlib `argparse`/`csv`, `pytest` (`uv run pytest`; sync tests use `asyncio.run()`).

**Scope:** read surface only. No changes to extraction/graph. Instrumentation, recompute, the reaper, and `finalize_research_run` wiring are **Plan 4**.

**Grounding (verified):** `ingest.py:72` INSERTs `fact (tuple_key, qualifiers_json, as_of, value, unit, source_id, admission, lifecycle, run_id, created_at)` with `qualifiers_json=json.dumps({})` and no instance/property. The `fact` table has unused `instance_id`/`property_id` columns. `source` table has `url_or_domain`, `tier`, `flags_json`. `conflict`/`conflict_member` link conflicted facts. `entities.CountryResolver` maps name→canonical key. No `[project.scripts]` exist.

---

### Task 1: Make facts queryable — store instance_key, property_name, real qualifiers

**Files:**
- Modify: `src/open_deep_research/factbase/schema.py` (append migration v3)
- Modify: `src/open_deep_research/factbase/ingest.py` (populate the new columns + real qualifiers_json)
- Test: `tests/test_factbase_ingest.py` (add a test)

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_factbase_ingest.py
def test_fact_row_stores_instance_property_and_qualifiers():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            ing = _setup(conn)   # the helper already in this test file
            recs = [{"property":"id_coverage_pct","instance_name":"India","value":"99","unit":"%","as_of":"2024",
                     "qualifiers":{"population_basis":"adults_15plus"},
                     "source_url":"https://id4d.worldbank.org/x","evidence_span":"99%"}]
            await ing.ingest(run_id=1, records=recs)
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT instance_key, property_name, qualifiers_json FROM fact")
            row = await cur.fetchone()
            assert row["instance_key"] == "IND"
            assert row["property_name"] == "id_coverage_pct"
            import json
            assert json.loads(row["qualifiers_json"]).get("population_basis") == "adults_15plus"
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_factbase_ingest.py::test_fact_row_stores_instance_property_and_qualifiers -v`
Expected: FAIL — `no such column: instance_key` (and then, once column exists, NULL/empty qualifiers).

- [ ] **Step 3: Implement**

Append a version-3 step to `schema.STEPS` in `schema.py`:
```python
    (3, """
    ALTER TABLE fact ADD COLUMN instance_key TEXT;
    ALTER TABLE fact ADD COLUMN property_name TEXT;
    CREATE INDEX IF NOT EXISTS ix_fact_instance ON fact(instance_key);
    CREATE INDEX IF NOT EXISTS ix_fact_property ON fact(property_name);
    """),
```

In `ingest.py`, where each fact is built/inserted: keep `instance_key` (the resolved canonical key) and `pd.name` and the real `quals` dict in scope (they already are — `instance_key` at line 31, `pd` and `quals` near line 39). Change the fact INSERT to also write `instance_key`, `property_name`, and the real qualifiers:
```python
    c = await self._conn.execute(
        "INSERT INTO fact (tuple_key, instance_key, property_name, qualifiers_json, as_of, value, unit, "
        "source_id, admission, lifecycle, run_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (tk, instance_key, pd.name, json.dumps(quals), as_of, f.value, f.unit, sid,
         "provisional", "current", run_id, now),
    )
```
(`json` is already imported in ingest.py; `quals` is the `{q: rec.qualifiers.get(q) for q in pd.identity_qualifiers}` dict already computed for the tuple_key.) Ensure `instance_key` and `pd` are in scope at the insert site — if the insert is inside a per-bucket loop that lost `instance_key`/`pd`, carry them in the bucket items tuple (the items already carry `rec`; recompute `pd = self._profile.property(rec["property"])` and re-resolve `instance_key = self._resolver.resolve(rec["instance_name"])` at the insert, or include them when building the bucket list). Whichever keeps the data correct.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_factbase_ingest.py -v`
Expected: PASS (existing ingest tests + the new one).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/schema.py src/open_deep_research/factbase/ingest.py tests/test_factbase_ingest.py
git commit -m "feat(factbase): store instance_key/property_name/qualifiers on fact rows (queryable)"
```

---

### Task 2: FactQuery — read facts for a country / a property

**Files:**
- Create: `src/open_deep_research/factbase/query.py`
- Test: `tests/test_factbase_query.py`

**Context:** Pure read adapter. `show(conn, instance_key)` returns the facts for one entity, joined to source (url/tier) and conflict status. `compare(conn, property_name)` returns facts across instances for one property. Each returned row is a plain dict the renderer formats.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_query.py
import asyncio, aiosqlite
from open_deep_research.factbase import migrations, schema, ingest, profile, entities, registry, query

DI = profile.load("country_digital_identity")

def _ing(conn):
    return ingest.Ingestor(conn, profile=DI, resolver=entities.CountryResolver(),
                           registry=registry.SourceRegistry.load("di_source_registry"))

def _seed(conn):
    return [
        {"property":"id_coverage_pct","instance_name":"India","value":"99","unit":"%","as_of":"2024",
         "qualifiers":{"population_basis":"adults_15plus"},"source_url":"https://id4d.worldbank.org/x","evidence_span":"99%"},
        {"property":"id_coverage_pct","instance_name":"India","value":"87","unit":"%","as_of":"2024",
         "qualifiers":{"population_basis":"adults_15plus"},"source_url":"https://gsma.com/y","evidence_span":"87%"},
    ]

def test_show_returns_facts_with_source_and_conflict():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            await _ing(conn).ingest(run_id=1, records=_seed(conn))
            rows = await query.FactQuery(conn).show("IND")
            assert len(rows) == 2
            assert {r["value"] for r in rows} == {"99", "87"}
            assert all(r["property_name"] == "id_coverage_pct" for r in rows)
            assert all(r["source_url"] for r in rows)            # joined source url present
            assert all(r["in_conflict"] for r in rows)            # both are in the open conflict
            assert all(r["admission"] == "provisional" for r in rows)
    asyncio.run(run())

def test_compare_groups_by_instance():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            await _ing(conn).ingest(run_id=1, records=_seed(conn))
            rows = await query.FactQuery(conn).compare("id_coverage_pct")
            assert all(r["instance_key"] == "IND" for r in rows)
            assert {r["value"] for r in rows} == {"99", "87"}
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_factbase_query.py -v`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement**

```python
# src/open_deep_research/factbase/query.py
"""Read-only fact-base queries for the dossier surface."""
from __future__ import annotations
import json
import aiosqlite


class FactQuery:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    async def _rows(self, where: str, params: tuple) -> list[dict]:
        self._conn.row_factory = aiosqlite.Row
        sql = (
            "SELECT f.id, f.instance_key, f.property_name, f.qualifiers_json, f.as_of, f.value, "
            "f.unit, f.admission, f.lifecycle, s.url_or_domain AS source_url, s.tier AS source_tier, "
            "EXISTS (SELECT 1 FROM conflict_member cm JOIN conflict c ON c.id=cm.conflict_id "
            "        WHERE cm.fact_id=f.id AND c.status='open') AS in_conflict "
            "FROM fact f LEFT JOIN source s ON s.id=f.source_id "
            f"WHERE f.soft_deleted_at IS NULL AND {where} "
            "ORDER BY f.property_name, f.as_of"
        )
        cur = await self._conn.execute(sql, params)
        out = []
        for r in await cur.fetchall():
            d = dict(r)
            d["qualifiers"] = json.loads(d.get("qualifiers_json") or "{}")
            d["in_conflict"] = bool(d["in_conflict"])
            out.append(d)
        return out

    async def show(self, instance_key: str) -> list[dict]:
        return await self._rows("f.instance_key = ?", (instance_key,))

    async def compare(self, property_name: str) -> list[dict]:
        return await self._rows("f.property_name = ?", (property_name,))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_factbase_query.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/query.py tests/test_factbase_query.py
git commit -m "feat(factbase): FactQuery (read-only show/compare with source + conflict)"
```

---

### Task 3: Renderer — one canonical render path (text/csv/md)

**Files:**
- Create: `src/open_deep_research/factbase/render.py`
- Test: `tests/test_factbase_render.py`

**Context:** Pure formatting of `FactQuery` rows. The rendering contract (Architecture §5): a provisional value is marked `~prov`; a conflicted value is marked `⚠`; neither is ever shown bare/established. Three formats: `text` (aligned table), `md` (markdown table), `csv`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_render.py
from open_deep_research.factbase import render

ROWS = [
    {"instance_key":"IND","property_name":"id_coverage_pct","qualifiers":{"population_basis":"adults_15plus"},
     "as_of":2024,"value":"99","unit":"%","admission":"provisional","in_conflict":True,
     "source_url":"https://id4d.worldbank.org/x","source_tier":"authoritative"},
    {"instance_key":"IND","property_name":"id_coverage_pct","qualifiers":{"population_basis":"adults_15plus"},
     "as_of":2024,"value":"87","unit":"%","admission":"provisional","in_conflict":True,
     "source_url":"https://gsma.com/y","source_tier":"authoritative"},
]

def test_text_marks_conflict_and_provisional():
    out = render.render(ROWS, fmt="text")
    assert "⚠" in out          # conflict marker present
    assert "~prov" in out       # provisional marker present
    assert "99" in out and "87" in out and "id4d.worldbank.org" in out

def test_csv_has_header_and_rows():
    out = render.render(ROWS, fmt="csv")
    lines = [l for l in out.splitlines() if l.strip()]
    assert lines[0].startswith("instance_key,property_name,")
    assert len(lines) == 3      # header + 2 rows

def test_md_is_a_table():
    out = render.render(ROWS, fmt="md")
    assert out.count("|") >= 6 and "---" in out

def test_empty_rows_message():
    assert "no facts" in render.render([], fmt="text").lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_factbase_render.py -v`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement**

```python
# src/open_deep_research/factbase/render.py
"""Canonical rendering for the dossier surface. Never present provisional/contested as established."""
from __future__ import annotations
import csv
import io

_COLUMNS = ["instance_key", "property_name", "qualifiers", "as_of", "value",
            "source_url", "source_tier", "status"]


def _status(row: dict) -> str:
    marks = []
    if row.get("in_conflict"):
        marks.append("⚠ in-conflict")
    if row.get("admission") != "trusted":
        marks.append("~prov")
    return " ".join(marks) if marks else "trusted"


def _cell(row: dict, col: str) -> str:
    if col == "status":
        return _status(row)
    if col == "qualifiers":
        return ";".join(f"{k}={v}" for k, v in (row.get("qualifiers") or {}).items())
    if col == "value":
        v = str(row.get("value", ""))
        u = row.get("unit") or ""
        return f"{v}{u}"
    return "" if row.get(col) is None else str(row.get(col))


def render(rows: list[dict], fmt: str = "text") -> str:
    if not rows:
        return "No facts found."
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(_COLUMNS)
        for r in rows:
            w.writerow([_cell(r, c) for c in _COLUMNS])
        return buf.getvalue()
    if fmt == "md":
        head = "| " + " | ".join(_COLUMNS) + " |"
        sep = "| " + " | ".join("---" for _ in _COLUMNS) + " |"
        body = ["| " + " | ".join(_cell(r, c) for c in _COLUMNS) + " |" for r in rows]
        return "\n".join([head, sep, *body])
    # text: aligned columns
    table = [_COLUMNS] + [[_cell(r, c) for c in _COLUMNS] for r in rows]
    widths = [max(len(table[i][j]) for i in range(len(table))) for j in range(len(_COLUMNS))]
    return "\n".join("  ".join(cell.ljust(widths[j]) for j, cell in enumerate(line)) for line in table)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_factbase_render.py -v`
Expected: PASS (all 4).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/render.py tests/test_factbase_render.py
git commit -m "feat(factbase): canonical renderer (text/md/csv; marks conflict + provisional)"
```

---

### Task 4: `dossier` CLI module

**Files:**
- Create: `src/open_deep_research/factbase/dossier.py`
- Modify: `pyproject.toml` (add `[project.scripts]` entry)
- Test: `tests/test_dossier_cli.py`

**Context:** `dossier show <country> [--format text|md|csv]` and `dossier compare <property> [--format ...]`. Resolves the country name → canonical key via `CountryResolver`, opens the DB read-only, queries, renders. A testable `run(argv, db_path) -> str` returns the rendered string; `main()` prints it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dossier_cli.py
import asyncio, aiosqlite
from open_deep_research.factbase import migrations, schema, ingest, profile, entities, registry, dossier

DI = profile.load("country_digital_identity")

def _seed_db(db_path):
    async def run():
        async with aiosqlite.connect(db_path) as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            recs = [
                {"property":"id_coverage_pct","instance_name":"India","value":"99","unit":"%","as_of":"2024",
                 "qualifiers":{"population_basis":"adults_15plus"},"source_url":"https://id4d.worldbank.org/x","evidence_span":"99%"},
                {"property":"id_coverage_pct","instance_name":"India","value":"87","unit":"%","as_of":"2024",
                 "qualifiers":{"population_basis":"adults_15plus"},"source_url":"https://gsma.com/y","evidence_span":"87%"},
            ]
            await ingest.Ingestor(conn, profile=DI, resolver=entities.CountryResolver(),
                                  registry=registry.SourceRegistry.load("di_source_registry")).ingest(run_id=1, records=recs)
    asyncio.run(run())

def test_show_renders_conflict(tmp_path):
    db = str(tmp_path / "f.db"); _seed_db(db)
    out = asyncio.run(dossier.run(["show", "India", "--format", "text"], db_path=db))
    assert "⚠" in out and "99" in out and "87" in out

def test_show_unknown_country(tmp_path):
    db = str(tmp_path / "f.db"); _seed_db(db)
    out = asyncio.run(dossier.run(["show", "Atlantis"], db_path=db))
    assert "unknown country" in out.lower() or "could not resolve" in out.lower()

def test_compare_csv(tmp_path):
    db = str(tmp_path / "f.db"); _seed_db(db)
    out = asyncio.run(dossier.run(["compare", "id_coverage_pct", "--format", "csv"], db_path=db))
    assert out.splitlines()[0].startswith("instance_key,")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_dossier_cli.py -v`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement**

```python
# src/open_deep_research/factbase/dossier.py
"""Read-only `dossier` CLI: show <country> / compare <property>."""
from __future__ import annotations
import argparse
import asyncio
import aiosqlite

from . import query as _query, render as _render
from .entities import CountryResolver
from open_deep_research.storage import get_db_path


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dossier", description="View the living fact base.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show", help="Show a country's fact table")
    s.add_argument("country")
    s.add_argument("--format", default="text", choices=["text", "md", "csv"])
    c = sub.add_parser("compare", help="Compare a property across countries")
    c.add_argument("property")
    c.add_argument("--format", default="text", choices=["text", "md", "csv"])
    return p


async def run(argv: list[str], db_path: str | None = None) -> str:
    args = _parser().parse_args(argv)
    db_path = db_path or get_db_path(None)
    async with aiosqlite.connect(db_path) as conn:
        q = _query.FactQuery(conn)
        if args.cmd == "show":
            key = CountryResolver().resolve(args.country)
            if key is None:
                return f"Unknown country: {args.country!r} (could not resolve to a canonical key)."
            rows = await q.show(key)
            return _render.render(rows, fmt=args.format)
        rows = await q.compare(args.property)
        return _render.render(rows, fmt=args.format)


def main() -> None:  # console-script entry
    import sys
    print(asyncio.run(run(sys.argv[1:])))


if __name__ == "__main__":
    main()
```

Add to `pyproject.toml` (create the section if absent):
```toml
[project.scripts]
dossier = "open_deep_research.factbase.dossier:main"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_dossier_cli.py -v`
Expected: PASS (all 3).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/dossier.py pyproject.toml tests/test_dossier_cli.py
git commit -m "feat(factbase): dossier CLI (show/compare, text/md/csv export)"
```

---

### Task 5: Full-suite check + manual-usage note

**Files:**
- Modify: `src/open_deep_research/factbase/__init__.py` (add a short module docstring listing the public surface — optional; skip if it grows churn)
- Test: (none new)

- [ ] **Step 1: Run the whole suite**

Run: `uv run pytest -q -p no:warnings`
Expected: all pass (foundation + Plan 2 + Plan 3 tests).

- [ ] **Step 2: Verify the CLI imports and parses**

Run: `uv run python -c "import open_deep_research.factbase.dossier as d; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit (only if __init__ docstring was added)**

```bash
git add src/open_deep_research/factbase/__init__.py
git commit -m "docs(factbase): note public surface in package docstring"
```

(If no file changed, skip the commit.)

---

## Plan 3 complete

After Task 5: a researcher can run `uv run dossier show India` / `dossier compare id_coverage_pct --format md` and see the accumulated facts — values, source + tier, as-of, qualifiers, and ⚠/~prov markers — with CSV/MD export for briefings. Facts are now stored queryably (instance_key/property_name/qualifiers), read by `FactQuery`, and formatted through one canonical render path that never presents provisional/contested facts as established.

**Open follow-ups → Plan 4 (and carried from Plan 2):**
- `finalize_research_run` wiring (orphan `running` rows) + stale-run reaper.
- Instrumentation: coverage / groundedness / false-conflict + drop-rate metrics; `coverage_incomplete` derivation; registry-version recompute pass.
- `required_qualifiers` subset on `PropertyDef`; the Plan-1 minors (AutoClose per-group as_of; `trust_threshold` REAL↔str; dataclass↔pydantic; `FactQuery`/`RunSourceStore` row_factory side effect).
- Verify the live `_make_fact_model_call` round-trip against a real backend + calibrate the extraction prompt.
- `dossier compare` could later split columns per qualifier-tuple (Feature Spec AC4.1); v1 lists rows per (instance, qualifier-set).
