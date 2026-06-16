# Country Population Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture a single numeric fact — population — for all countries, loaded directly from World Bank data into the factbase as `trusted` facts.

**Architecture:** Add a first-class `number` value-kind (3 seams parallel to `percentage`), a one-property `country_population` profile + a World Bank source registry, an author-time generator that vendors `data/population.yaml`, and a loader that feeds the existing `Ingestor` (reusing canonicalization/tiering/promotion). Surfaced as `dossier population-load`; viewed via `dossier matrix --profile country_population`.

**Tech Stack:** Python 3.11, `aiosqlite`, `PyYAML`, stdlib `urllib` (author-time fetch), `pytest` with `asyncio.run()` (codebase convention — NOT pytest-asyncio).

**Spec:** `docs/superpowers/specs/2026-06-16-country-population-design.md`.

**Branch:** continue on `feat/country-population`.

---

## File Structure

- Modify `factbase/profile_schema.py` — `_VALID_KINDS` += `"number"`.
- Modify `factbase/profile.py` — `PropertyDef.validate` number branch.
- Modify `factbase/identity.py` — `canonical_value` number branch.
- Create `factbase/profiles/country_population.yaml` — the profile.
- Create `factbase/profiles/country_population_source_registry.yaml` — World Bank tiers.
- Create `scripts/gen_population.py` — author-time World Bank fetch → `data/population.yaml`.
- Create `factbase/data/population.yaml` — generated, committed.
- Create `factbase/population_loader.py` — `load_population`.
- Modify `factbase/dossier.py` — `population-load` subcommand.
- Tests: `test_factbase_number_kind.py`, `test_country_population_profile.py`, `test_population_loader.py`, `test_dossier_population_load.py`.

---

### Task 1: `number` value-kind

**Files:**
- Modify: `src/open_deep_research/factbase/profile_schema.py:17`
- Modify: `src/open_deep_research/factbase/profile.py` (`PropertyDef.validate`)
- Modify: `src/open_deep_research/factbase/identity.py` (`canonical_value`)
- Test: `tests/test_factbase_number_kind.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_factbase_number_kind.py`:

```python
from open_deep_research.factbase.identity import canonical_value
from open_deep_research.factbase.profile import PropertyDef
from open_deep_research.factbase.profile_schema import profile_from_dict


def test_meta_schema_accepts_number_kind():
    prof = profile_from_dict({
        "entity_type": "country", "version": "1",
        "properties": [{"name": "population", "kind": "number", "description": "total"}],
    })
    assert prof.property("population").value_kind == "number"


def test_validate_number_accepts_separators_rejects_text():
    pd = PropertyDef(name="population", value_kind="number")
    assert pd.validate("1402000000") is True
    assert pd.validate("1,402,000,000") is True
    assert pd.validate("  1_402_000_000 ") is True
    assert pd.validate("12.5") is True
    assert pd.validate("abc") is False
    assert pd.validate("") is False


def test_canonical_number_collapses_separators_and_integral():
    pd = PropertyDef(name="population", value_kind="number")
    a, _ = canonical_value(pd, "1,402,000,000", None)
    b, _ = canonical_value(pd, "1402000000", None)
    assert a == b == "1402000000"          # separators stripped, integral form
    c, _ = canonical_value(pd, "12.50", None)
    assert c == "12.5"                       # non-integral normalized
    d, _ = canonical_value(pd, "not a number", None)
    assert d == "not a number"              # non-numeric falls back to text norm
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_factbase_number_kind.py -q`
Expected: FAIL — meta-schema rejects `number` (unknown kind), `validate`/`canonical_value` treat it as a plain name.

- [ ] **Step 3: Add `number` to the valid kinds**

In `src/open_deep_research/factbase/profile_schema.py`, line 17, change:
```python
_VALID_KINDS = {"name", "enum", "percentage", "boolean", "name_year"}
```
to:
```python
_VALID_KINDS = {"name", "enum", "percentage", "boolean", "name_year", "number"}
```

- [ ] **Step 4: Add the `validate` number branch**

In `src/open_deep_research/factbase/profile.py`, in `PropertyDef.validate`, after the `percentage` branch and before the `enum` branch, add:
```python
        if self.value_kind == "number":
            s = v.replace(",", "").replace("_", "").replace(" ", "")
            try:
                float(s)
                return True
            except ValueError:
                return False
```
(`v` is the already-stripped value at the top of `validate`.)

- [ ] **Step 5: Add the `canonical_value` number branch**

In `src/open_deep_research/factbase/identity.py`, inside `canonical_value`, add a branch before the `name`/`name_year` branch (after `boolean`):
```python
    if kind == "number":
        s = raw.replace(",", "").replace("_", "").replace(" ", "")
        try:
            f = float(s)
        except ValueError:
            return (_norm_text(raw), _norm_text(unit) or None)
        canon = str(int(f)) if f == int(f) else repr(f)
        return (canon, _norm_text(unit) or None)
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/test_factbase_number_kind.py -q`
Expected: PASS (3 tests).

- [ ] **Step 7: Regression — full suite**

Run: `uv run pytest tests/ -p no:warnings`
Expected: green (existing kinds unchanged; `number` is additive).

- [ ] **Step 8: Commit**

```bash
git add src/open_deep_research/factbase/profile_schema.py src/open_deep_research/factbase/profile.py \
        src/open_deep_research/factbase/identity.py tests/test_factbase_number_kind.py
git commit -m "feat(factbase): add numeric 'number' value-kind (validate + canonicalization)"
```

---

### Task 2: `country_population` profile + World Bank registry

**Files:**
- Create: `src/open_deep_research/factbase/profiles/country_population.yaml`
- Create: `src/open_deep_research/factbase/profiles/country_population_source_registry.yaml`
- Test: `tests/test_country_population_profile.py`

- [ ] **Step 1: Create the profile**

`src/open_deep_research/factbase/profiles/country_population.yaml`:
```yaml
entity_type: country
version: '1'
properties:
- name: population
  kind: number
  description: Total population (most recent World Bank SP.POP.TOTL estimate).
  trust_threshold: reputable
```

- [ ] **Step 2: Create the source registry**

`src/open_deep_research/factbase/profiles/country_population_source_registry.yaml`:
```yaml
version: '1'
sources:
- domain: data.worldbank.org
  tier: authoritative
  flags: [primary]
- domain: api.worldbank.org
  tier: authoritative
  flags: [primary]
- domain: worldbank.org
  tier: authoritative
  flags: []
```

- [ ] **Step 3: Write the test**

Create `tests/test_country_population_profile.py`:
```python
from open_deep_research.factbase.profile import load as load_profile
from open_deep_research.factbase.registry import SourceRegistry


def test_population_profile_loads():
    prof = load_profile("country_population")
    assert prof.entity_type == "country"
    assert prof.property("population").value_kind == "number"


def test_population_registry_tiers_world_bank_authoritative():
    reg = SourceRegistry.load("country_population_source_registry")
    assert reg.tier("https://data.worldbank.org/indicator/SP.POP.TOTL") == "authoritative"
    assert reg.meets_bar("https://data.worldbank.org/x", "reputable") is True
```

- [ ] **Step 4: Run the test**

Run: `uv run pytest tests/test_country_population_profile.py -q`
Expected: PASS (2 tests). (Depends on Task 1's `number` kind being merged.)

- [ ] **Step 5: Validate via the CLI**

Run: `uv run dossier validate`
Expected output contains `OK    country_population.yaml` and `OK    country_population_source_registry.yaml`; no `INVALID`.

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/factbase/profiles/country_population.yaml \
        src/open_deep_research/factbase/profiles/country_population_source_registry.yaml \
        tests/test_country_population_profile.py
git commit -m "feat(factbase): country_population profile + World Bank source registry"
```

---

### Task 3: Author-time generator + vendored `population.yaml`

**Files:**
- Create: `scripts/gen_population.py`
- Create: `src/open_deep_research/factbase/data/population.yaml` (generated)
- Test: `tests/test_population_data.py`

- [ ] **Step 1: Write the generator**

Create `scripts/gen_population.py`:
```python
"""Regenerate factbase/data/population.yaml from the World Bank API (author-time only).

Run: uv run python scripts/gen_population.py
Pulls SP.POP.TOTL most-recent-non-empty value per economy, keeps ISO-3166 alpha-3
countries (drops aggregates/regions), writes {ALPHA3: {value, year}}. Runtime never
imports this; it reads the committed YAML.
"""
import json
import os
import urllib.request

import yaml

URL = ("https://api.worldbank.org/v2/country/all/indicator/SP.POP.TOTL"
       "?format=json&mrnev=1&per_page=400")
OUT = os.path.join(os.path.dirname(__file__), "..", "src", "open_deep_research",
                   "factbase", "data", "population.yaml")


def main() -> None:
    with urllib.request.urlopen(URL, timeout=60) as resp:  # noqa: S310 - fixed trusted URL
        payload = json.load(resp)
    rows = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
    out = {}
    for r in rows:
        code = (r.get("countryiso3code") or "").strip()
        val = r.get("value")
        year = r.get("date")
        # Keep only real alpha-3 country codes with a value; WB aggregates have non-ISO codes.
        if len(code) == 3 and code.isalpha() and val is not None and year:
            out[code] = {"value": int(val), "year": int(year)}
    with open(os.path.normpath(OUT), "w", encoding="utf-8") as fh:
        yaml.safe_dump(out, fh, sort_keys=True)
    print(f"wrote {len(out)} countries to {os.path.normpath(OUT)}")  # noqa: T201


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Generate the data (needs outbound HTTPS once)**

Run: `uv run python scripts/gen_population.py`
Expected: `wrote <N> countries to .../population.yaml` (N ≈ 200–217).

**If the sandbox blocks outbound HTTPS** (`urlopen` raises a URLError/timeout): do NOT fabricate data. Report BLOCKED with the error; the controller will run the one-liner on a networked host (or supply the file) and the rest of the task proceeds. The generated YAML is small and reviewable in the diff.

- [ ] **Step 3: Spot-check the generated data**

Run: `grep -E "^(IND|USA|CHN|NRU|TUV):" -A2 src/open_deep_research/factbase/data/population.yaml`
Expected: IND/USA/CHN present with plausible large values; NRU/TUV (tiny states) present with small values. No aggregate codes (e.g. `WLD`, `EUU`) — confirm with `grep -E "^(WLD|EUU|ARB):" src/.../population.yaml` returning nothing.

- [ ] **Step 4: Write the data test**

Create `tests/test_population_data.py`:
```python
from importlib.resources import files

import yaml


def test_population_data_is_country_keyed_with_values():
    text = files("open_deep_research.factbase.data").joinpath("population.yaml").read_text(
        encoding="utf-8")
    data = yaml.safe_load(text)
    assert len(data) > 150                         # broad country coverage
    assert "IND" in data and "USA" in data
    ind = data["IND"]
    assert isinstance(ind["value"], int) and ind["value"] > 1_000_000_000
    assert isinstance(ind["year"], int) and 2000 <= ind["year"] <= 2100
    assert "WLD" not in data                        # no World Bank aggregates
```

- [ ] **Step 5: Run the test**

Run: `uv run pytest tests/test_population_data.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/gen_population.py src/open_deep_research/factbase/data/population.yaml \
        tests/test_population_data.py
git commit -m "feat(factbase): vendor World Bank population.yaml + author-time generator"
```

---

### Task 4: Population loader

**Files:**
- Create: `src/open_deep_research/factbase/population_loader.py`
- Test: `tests/test_population_loader.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_population_loader.py`:
```python
import asyncio
import sqlite3

from open_deep_research.factbase.population_loader import load_population


def test_load_population_ingests_trusted_facts(tmp_path):
    db = str(tmp_path / "pop.db")
    # tiny in-memory dataset override (no network, no real population.yaml)
    data = {"NGA": {"value": 223804632, "year": 2023},
            "BHS": {"value": 412623, "year": 2023}}
    result = asyncio.run(load_population(db, data=data))

    assert result["loaded"] == 2
    assert result["instances"] == 2
    assert result["trusted"] >= 1          # World Bank authoritative -> promoted

    conn = sqlite3.connect(db)
    rows = dict(conn.execute(
        "SELECT instance_key, value FROM fact WHERE property_name='population'").fetchall())
    assert rows["NGA"] == "223804632"
    trusted = conn.execute(
        "SELECT COUNT(*) FROM fact WHERE property_name='population' AND admission='trusted'"
    ).fetchone()[0]
    assert trusted >= 1
    # provenance: source is a World Bank domain
    src = conn.execute(
        "SELECT s.url_or_domain FROM fact f JOIN source s ON s.id=f.source_id "
        "WHERE f.property_name='population' LIMIT 1").fetchone()[0]
    assert "worldbank.org" in src
    conn.close()


def test_load_population_reports_unresolved(tmp_path):
    db = str(tmp_path / "pop2.db")
    data = {"NGA": {"value": 1, "year": 2023}, "ZZZ": {"value": 2, "year": 2023}}
    result = asyncio.run(load_population(db, data=data))
    assert "ZZZ" in result["skipped"]      # not a resolvable ISO country -> reported
    assert result["loaded"] == 1
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_population_loader.py -q`
Expected: FAIL — module `population_loader` missing.

- [ ] **Step 3: Implement the loader**

Create `src/open_deep_research/factbase/population_loader.py`:
```python
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

    text = files("open_deep_research.factbase.data").joinpath("population.yaml").read_text(
        encoding="utf-8")
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
            "FROM fact WHERE property_name='population' AND run_id=?", (str(run_id),))
        r = await cur.fetchone()
    return {"loaded": r["n"] or 0, "trusted": r["t"] or 0,
            "instances": r["k"] or 0, "skipped": skipped}
```

NOTE for the implementer: confirm against `ingest.py` how `run_id` is stored on `fact` (the smoke DB showed `fact.run_id` as a string, hence the `run_id=?` bound as `str(run_id)` above). If `ingest` stores it as an int, drop the `str(...)`. Verify with a quick read of `ingest.py` before finalizing the count query; adjust the bind type to match, and make the test assertion robust either way (the test reads `value`/`admission`, not run_id, so it passes regardless — only the returned counts depend on this).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_population_loader.py -q`
Expected: PASS (2 tests). If `trusted` is 0, re-read how `promotion`/`source_meets_bar` treats a single authoritative source in `ingest.py` and confirm the registry domain matches `_SOURCE_URL`'s host (`data.worldbank.org`).

- [ ] **Step 5: Full suite**

Run: `uv run pytest tests/ -p no:warnings`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/factbase/population_loader.py tests/test_population_loader.py
git commit -m "feat(factbase): direct population loader (World Bank -> trusted facts via Ingestor)"
```

---

### Task 5: `dossier population-load` subcommand

**Files:**
- Modify: `src/open_deep_research/factbase/dossier.py`
- Test: `tests/test_dossier_population_load.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_dossier_population_load.py`:
```python
import asyncio
import sqlite3

from open_deep_research.factbase.dossier import run


def test_population_load_subcommand(tmp_path, monkeypatch):
    db = str(tmp_path / "pl.db")
    # stub the data read so the CLI test needs no network / real population.yaml
    from open_deep_research.factbase import population_loader as pl
    monkeypatch.setattr(pl, "_load_data",
                        lambda: {"NGA": {"value": 223804632, "year": 2023}})
    out = asyncio.run(run(["population-load"], db_path=db))
    assert "loaded 1" in out and "trusted" in out

    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM fact WHERE property_name='population'").fetchone()[0]
    conn.close()
    assert n == 1
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_dossier_population_load.py -q`
Expected: FAIL — `invalid choice: 'population-load'`.

- [ ] **Step 3: Add the subparser**

In `src/open_deep_research/factbase/dossier.py` `_parser()`, after the `batch` parser block, add:
```python
    sub.add_parser("population-load",
                   help="Load country population from vendored World Bank data into the fact base.")
```

- [ ] **Step 4: Add the handler**

In `dossier.py` `run()`, add a branch alongside the others (before the final `async with` fallthrough):
```python
    if args.command == "population-load":
        from .population_loader import load_population
        res = await load_population(db_path)
        return (f"loaded {res['loaded']} ({res['trusted']} trusted) across "
                f"{res['instances']} countries"
                + (f" | skipped: {', '.join(res['skipped'])}" if res['skipped'] else ""))
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_dossier_population_load.py -q`
Expected: PASS.

- [ ] **Step 6: End-to-end view (manual, optional)**

Run: `RESEARCH_DB_PATH=/tmp/pop.db uv run dossier population-load`
Then: `RESEARCH_DB_PATH=/tmp/pop.db uv run dossier matrix --profile country_population --format md | head`
Expected: a `population` column populated with `*` (trusted) markers across countries. (Skip if `population.yaml` wasn't generated due to the network block — the unit tests cover the logic.)

- [ ] **Step 7: Full suite + lint-awareness**

Run: `uv run pytest tests/ -p no:warnings`
Expected: green.

- [ ] **Step 8: Commit**

```bash
git add src/open_deep_research/factbase/dossier.py tests/test_dossier_population_load.py
git commit -m "feat(dossier): population-load subcommand"
```

---

## Self-Review

**Spec coverage:**
- `number` kind (3 seams) → Task 1. ✓ (US-1, US-2)
- `country_population` profile + World Bank registry → Task 2. ✓ (US-1, US-4 substrate)
- Vendored data + generator → Task 3. ✓
- Loader feeding Ingestor, trusted promotion, no-silent-drop → Task 4. ✓ (US-3, US-4, US-6)
- `dossier population-load` + matrix view → Task 5. ✓ (US-5)

**Placeholder scan:** No TBD/TODO; every code step is complete. The two "confirm against ingest.py" notes are concrete verification steps (run_id bind type), not placeholders — the test passes regardless of the bind detail.

**Type consistency:** `load_population(db_path, *, profile_name, registry_name, data) -> dict` with keys `loaded/trusted/instances/skipped` used identically in Tasks 4 and 5. `Ingestor(conn, *, profile, resolver, registry)` + `ingest(run_id, records)` match the real signatures. Record dict keys (`property`, `instance_name`, `value`, `as_of`, `source_url`, `evidence_span`) match `ingest.py`'s `rec[...]`/`rec.get(...)` reads. `_load_data` is the monkeypatch seam used in Task 5's CLI test.

**Network caveat (Task 3):** the only step needing outbound HTTPS is `gen_population.py`. If blocked in-sandbox, the implementer reports BLOCKED for that step only; the controller runs the generator on a networked host. All other tasks/tests are network-free (tests inject `data`).
