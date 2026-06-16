# Multi-Country Batch Research — Plan 7a (Foundation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make per-country research cover *any* country (not a hardcoded 20) and render a cross-country comparison matrix — the data foundation the batch orchestration (Plan 7b) builds on.

**Architecture:** Three independent, data-driven units, no graph changes. (1) Rebuild `CountryResolver` to load ISO-3166 from a vendored YAML (fixes the silent fact-drop blocker), preserving the `resolve(name) -> str | None` contract. (2) Add a `CountryListResolver` that expands explicit/group inputs into name lists (scout deferred to 7b where the model call lives). (3) Add a matrix renderer + `dossier matrix` subcommand reusing the existing `FactQuery`/`group_by_canonical`.

**Tech Stack:** Python 3.11, `aiosqlite`, `PyYAML`, `pytest`/`pytest-asyncio`, `importlib.resources`. `pycountry` is an **author-time-only** dependency used by a one-off generator script; runtime reads the committed YAML.

**Spec:** `docs/superpowers/specs/2026-06-16-multi-country-batch-research-design.md` (Units 1, 2, 5).

**Branch:** continue on `spec/multi-country-batch`.

---

## File Structure

- Create `src/open_deep_research/factbase/data/__init__.py` — make `data` a package subdir for `importlib.resources`.
- Create `src/open_deep_research/factbase/data/iso3166.yaml` — alpha-3 → list of name aliases (generated once, committed).
- Create `src/open_deep_research/factbase/data/groups.yaml` — named group → list of country names.
- Create `scripts/gen_iso3166.py` — author-time generator (uses `pycountry`) that writes `iso3166.yaml`. Not imported at runtime.
- Modify `src/open_deep_research/factbase/entities.py` — `CountryResolver` loads the YAML; same signature; adds `instance_name(key)` reverse lookup for matrix row labels.
- Create `src/open_deep_research/factbase/country_list.py` — `resolve_country_list(spec)` (explicit / `@file` / group).
- Create `src/open_deep_research/factbase/matrix.py` — `build_matrix(...)` + `render_matrix(...)`.
- Modify `src/open_deep_research/factbase/dossier.py` — add the `matrix` subcommand.
- Modify `pyproject.toml` — add `pycountry` to the `dev` extra; ensure `factbase.data` is packaged.
- Tests: `tests/test_entity_resolver_iso.py`, `tests/test_country_list_resolver.py`, `tests/test_matrix_render.py`, `tests/test_dossier_matrix.py`, `tests/test_factbase_data_packaging.py`.

---

### Task 1: Vendor the ISO-3166 data file + packaging

**Files:**
- Create: `src/open_deep_research/factbase/data/__init__.py`
- Create: `scripts/gen_iso3166.py`
- Create: `src/open_deep_research/factbase/data/iso3166.yaml` (generated)
- Modify: `pyproject.toml`
- Test: `tests/test_factbase_data_packaging.py`

- [ ] **Step 1: Create the data package marker**

Create `src/open_deep_research/factbase/data/__init__.py` with a single line:

```python
"""Bundled data files (ISO-3166 countries, named groups) read via importlib.resources."""
```

- [ ] **Step 2: Write the author-time generator**

Create `scripts/gen_iso3166.py`:

```python
"""Regenerate factbase/data/iso3166.yaml from pycountry (author-time only).

Run: uv run --with pycountry python scripts/gen_iso3166.py
Runtime never imports this; it reads the committed YAML.
"""
import os
import unicodedata

import pycountry
import yaml

OUT = os.path.join(os.path.dirname(__file__), "..", "src", "open_deep_research",
                   "factbase", "data", "iso3166.yaml")

# Hand-maintained common aliases/exonyms not in pycountry's primary name.
ALIASES = {
    "USA": ["United States", "US", "USA", "America"],
    "GBR": ["United Kingdom", "UK", "Britain", "Great Britain"],
    "KOR": ["South Korea", "Korea"],
    "PRK": ["North Korea"],
    "ARE": ["United Arab Emirates", "UAE"],
    "RUS": ["Russia"],
    "TUR": ["Turkey", "Turkiye", "Türkiye"],
    "CIV": ["Ivory Coast", "Cote d'Ivoire", "Côte d'Ivoire"],
    "CZE": ["Czech Republic", "Czechia"],
    "VEN": ["Venezuela"],
    "BOL": ["Bolivia"],
    "IRN": ["Iran"],
    "SYR": ["Syria"],
    "LAO": ["Laos"],
    "TZA": ["Tanzania"],
    "VNM": ["Vietnam"],
}


def main() -> None:
    out = {}
    for c in pycountry.countries:
        names = [c.name]
        for attr in ("official_name", "common_name"):
            v = getattr(c, attr, None)
            if v and v not in names:
                names.append(v)
        names.extend(a for a in ALIASES.get(c.alpha_3, []) if a not in names)
        out[c.alpha_3] = names
    with open(os.path.normpath(OUT), "w", encoding="utf-8") as fh:
        yaml.safe_dump(out, fh, allow_unicode=True, sort_keys=True)
    print(f"wrote {len(out)} countries to {os.path.normpath(OUT)}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Generate the YAML and verify the canonical cases**

Run: `uv run --with pycountry python scripts/gen_iso3166.py`
Expected: `wrote 249 countries to .../iso3166.yaml` (count may be 249±a few by pycountry version).

Then verify the blocker cases are present:
Run: `grep -E "^(BHS|CHN|JAM|GHA|NGA):" src/open_deep_research/factbase/data/iso3166.yaml`
Expected: all four lines print (Bahamas/China/Jamaica/Ghana/Nigeria now covered).

- [ ] **Step 4: Package the data dir + add author dep**

In `pyproject.toml`, add `pycountry` to the `dev` extra's dependency list (author-time only).

Ensure the data files ship in wheels. Find the existing `[tool.setuptools.package-data]` (or `[tool.setuptools.packages]`) block. Add `open_deep_research.factbase.data` to the packages list **and** a package-data glob so `*.yaml` is included:

```toml
[tool.setuptools.package-data]
"open_deep_research.factbase.profiles" = ["*.yaml"]
"open_deep_research.factbase.data" = ["*.yaml"]
```

If `[tool.setuptools.packages]` enumerates packages explicitly, add `"open_deep_research.factbase.data"` to it (mirrors how `factbase.profiles` was added in the profiles-as-data work — see that commit if unsure).

- [ ] **Step 5: Write the packaging test**

Create `tests/test_factbase_data_packaging.py`:

```python
from importlib.resources import files


def test_iso3166_and_groups_are_importable_resources():
    pkg = files("open_deep_research.factbase.data")
    iso = pkg.joinpath("iso3166.yaml").read_text(encoding="utf-8")
    assert "BHS" in iso and "NGA" in iso  # blocker cases covered
    groups = pkg.joinpath("groups.yaml").read_text(encoding="utf-8")
    assert "G20" in groups
```

(`groups.yaml` is created in Task 3; this test will pass once both exist — if running Task 1 in isolation, expect the groups assertion to fail until Task 3.)

- [ ] **Step 6: Commit**

```bash
git add scripts/gen_iso3166.py src/open_deep_research/factbase/data/__init__.py \
        src/open_deep_research/factbase/data/iso3166.yaml pyproject.toml \
        tests/test_factbase_data_packaging.py
git commit -m "feat(factbase): vendor ISO-3166 country data + package data dir"
```

---

### Task 2: Rebuild CountryResolver to load ISO-3166 from data

**Files:**
- Modify: `src/open_deep_research/factbase/entities.py`
- Test: `tests/test_entity_resolver_iso.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_entity_resolver_iso.py`:

```python
from open_deep_research.factbase.entities import CountryResolver


def test_resolves_iso_names_and_aliases():
    r = CountryResolver()
    assert r.resolve("Bahamas") == "BHS"        # the blocker case
    assert r.resolve("Nigeria") == "NGA"        # original 20 still work
    assert r.resolve("United Kingdom") == "GBR"
    assert r.resolve("UK") == "GBR"             # alias
    assert r.resolve("Türkiye") == "TUR"        # diacritics + endonym
    assert r.resolve("south korea") == "KOR"    # case-insensitive


def test_unresolved_returns_none():
    r = CountryResolver()
    assert r.resolve("Atlantis") is None
    assert r.resolve("") is None


def test_instance_name_reverse_lookup():
    r = CountryResolver()
    assert r.instance_name("BHS") == "Bahamas"  # primary name for matrix labels
    assert r.instance_name("ZZZ") == "ZZZ"      # unknown key -> echo the key
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/test_entity_resolver_iso.py -q`
Expected: FAIL — `resolve("Bahamas")` returns `None` (old dict) and `instance_name` doesn't exist (`AttributeError`).

- [ ] **Step 3: Rebuild entities.py**

Replace the entire contents of `src/open_deep_research/factbase/entities.py` with:

```python
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

_NORM = re.compile(r"[^a-z0-9]+")


def _norm(s: str) -> str:
    # Fold diacritics (ü -> u, ô -> o) so aliases match regardless of accents.
    decomposed = unicodedata.normalize("NFKD", s or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NORM.sub("", stripped.lower())


@lru_cache(maxsize=1)
def _load() -> tuple[dict[str, str], dict[str, str]]:
    """Return (norm_name -> alpha3, alpha3 -> primary_name), loaded once from data."""
    import yaml
    from importlib.resources import files

    text = files("open_deep_research.factbase.data").joinpath("iso3166.yaml").read_text(
        encoding="utf-8")
    data = yaml.safe_load(text) or {}
    name_to_key: dict[str, str] = {}
    key_to_name: dict[str, str] = {}
    for alpha3, names in data.items():
        if not names:
            continue
        key_to_name[alpha3] = names[0]  # first entry is the primary display name
        for n in names:
            name_to_key.setdefault(_norm(n), alpha3)
    return name_to_key, key_to_name


class CountryResolver:
    def resolve(self, name: str) -> str | None:
        return _load()[0].get(_norm(name))

    def instance_name(self, key: str) -> str:
        """Primary display name for an alpha-3 key (echoes the key if unknown)."""
        return _load()[1].get(key, key)
```

- [ ] **Step 4: Run the test to confirm it passes**

Run: `uv run pytest tests/test_entity_resolver_iso.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Confirm no regression in ingest / existing factbase tests**

Run: `uv run pytest tests/ -q -k "factbase or ingest or knowledge"`
Expected: PASS — the contract `resolve(name) -> str | None` is unchanged, so `ingest.py` is unaffected; the original 20 countries still resolve.

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/factbase/entities.py tests/test_entity_resolver_iso.py
git commit -m "feat(factbase): resolve any ISO-3166 country from data, not a hardcoded 20"
```

---

### Task 3: CountryListResolver (explicit / @file / group)

**Files:**
- Create: `src/open_deep_research/factbase/data/groups.yaml`
- Create: `src/open_deep_research/factbase/country_list.py`
- Test: `tests/test_country_list_resolver.py`

- [ ] **Step 1: Create the groups data file**

Create `src/open_deep_research/factbase/data/groups.yaml`:

```yaml
# Named country groups -> member country names (resolved via CountryResolver at use time).
# Editable data, not code. Keep groups globally representative, not Western-only.
G20:
  - Argentina
  - Australia
  - Brazil
  - Canada
  - China
  - France
  - Germany
  - India
  - Indonesia
  - Italy
  - Japan
  - Mexico
  - Russia
  - Saudi Arabia
  - South Africa
  - South Korea
  - Turkey
  - United Kingdom
  - United States
West Africa:
  - Nigeria
  - Ghana
  - Senegal
  - Cote d'Ivoire
  - Benin
  - Togo
  - Mali
  - Niger
  - Burkina Faso
  - Guinea
EU:
  - Austria
  - Belgium
  - Bulgaria
  - Croatia
  - Cyprus
  - Czech Republic
  - Denmark
  - Estonia
  - Finland
  - France
  - Germany
  - Greece
  - Hungary
  - Ireland
  - Italy
  - Latvia
  - Lithuania
  - Luxembourg
  - Malta
  - Netherlands
  - Poland
  - Portugal
  - Romania
  - Slovakia
  - Slovenia
  - Spain
  - Sweden
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_country_list_resolver.py`:

```python
import pytest

from open_deep_research.factbase.country_list import resolve_country_list


def test_explicit_comma_list():
    assert resolve_country_list("Nigeria, India ,Bahamas") == ["Nigeria", "India", "Bahamas"]


def test_at_file(tmp_path):
    p = tmp_path / "list.txt"
    p.write_text("Nigeria\nIndia\n\n  Bahamas  \n", encoding="utf-8")
    assert resolve_country_list(f"@{p}") == ["Nigeria", "India", "Bahamas"]


def test_named_group_expands():
    out = resolve_country_list("G20")
    assert "China" in out and "India" in out and len(out) == 19


def test_unknown_group_treated_as_single_name():
    # A bare token that is not a known group is treated as one explicit country name.
    assert resolve_country_list("Atlantis") == ["Atlantis"]


def test_empty_raises():
    with pytest.raises(ValueError):
        resolve_country_list("   ")
```

- [ ] **Step 3: Run it to confirm it fails**

Run: `uv run pytest tests/test_country_list_resolver.py -q`
Expected: FAIL — module `country_list` does not exist (ImportError).

- [ ] **Step 4: Implement country_list.py**

Create `src/open_deep_research/factbase/country_list.py`:

```python
"""Expand a CLI country-list spec into a list of country names.

Three input shapes (scout discovery lives in Plan 7b, where the model call is):
  - "@/path/to/file"   one country name per line
  - a known group name ("G20", "EU", "West Africa") -> its members
  - a comma-separated list of names ("A, B, C")
A single bare token that is not a known group is treated as one explicit name.
"""
from __future__ import annotations


def _load_groups() -> dict[str, list[str]]:
    import yaml
    from importlib.resources import files

    text = files("open_deep_research.factbase.data").joinpath("groups.yaml").read_text(
        encoding="utf-8")
    return yaml.safe_load(text) or {}


def resolve_country_list(spec: str) -> list[str]:
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("empty country-list spec")
    if spec.startswith("@"):
        with open(spec[1:], encoding="utf-8") as fh:
            names = [ln.strip() for ln in fh]
        out = [n for n in names if n]
        if not out:
            raise ValueError(f"no country names in file {spec[1:]}")
        return out
    if "," not in spec:
        groups = _load_groups()
        if spec in groups:
            return list(groups[spec])
        return [spec]  # a single explicit name
    return [part.strip() for part in spec.split(",") if part.strip()]
```

- [ ] **Step 5: Run the test to confirm it passes**

Run: `uv run pytest tests/test_country_list_resolver.py -q`
Expected: PASS (5 tests).

- [ ] **Step 6: Re-run the packaging test (now groups.yaml exists)**

Run: `uv run pytest tests/test_factbase_data_packaging.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/open_deep_research/factbase/data/groups.yaml \
        src/open_deep_research/factbase/country_list.py \
        tests/test_country_list_resolver.py
git commit -m "feat(factbase): country-list resolver (explicit/@file/group)"
```

---

### Task 4: Matrix builder + renderer

**Files:**
- Create: `src/open_deep_research/factbase/matrix.py`
- Test: `tests/test_matrix_render.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_matrix_render.py`:

```python
from open_deep_research.factbase.matrix import build_matrix, render_matrix


def _grouped(instance_key, property_name, value, admission="provisional"):
    # Shape mirrors query.group_by_canonical output rows.
    return {"instance_key": instance_key, "property_name": property_name,
            "value": value, "admission": admission, "in_conflict": False}


def test_build_matrix_rows_by_instance_cols_by_property():
    rows = [
        _grouped("NGA", "cbdc_launch_status", "launched", "trusted"),
        _grouped("NGA", "cbdc_ledger_architecture", "centralized"),
        _grouped("IND", "cbdc_launch_status", "pilot"),
    ]
    m = build_matrix(rows, ["cbdc_launch_status", "cbdc_ledger_architecture"],
                     label=lambda k: {"NGA": "Nigeria", "IND": "India"}[k])
    # ordered by instance label
    assert [r["instance"] for r in m] == ["India", "Nigeria"]
    nga = next(r for r in m if r["instance"] == "Nigeria")
    assert nga["cells"]["cbdc_launch_status"] == "launched*"     # * marks trusted
    assert nga["cells"]["cbdc_ledger_architecture"] == "centralized"
    ind = next(r for r in m if r["instance"] == "India")
    assert ind["cells"]["cbdc_ledger_architecture"] == ""        # coverage gap


def test_render_markdown_has_header_and_rows():
    rows = [_grouped("NGA", "cbdc_launch_status", "launched", "trusted")]
    out = render_matrix(rows, ["cbdc_launch_status"], lambda k: "Nigeria", fmt="md")
    assert "| country | cbdc_launch_status |" in out
    assert "| Nigeria | launched* |" in out


def test_render_csv():
    rows = [_grouped("NGA", "cbdc_launch_status", "launched")]
    out = render_matrix(rows, ["cbdc_launch_status"], lambda k: "Nigeria", fmt="csv")
    assert out.splitlines()[0] == "country,cbdc_launch_status"
    assert out.splitlines()[1] == "Nigeria,launched"
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/test_matrix_render.py -q`
Expected: FAIL — module `matrix` does not exist (ImportError).

- [ ] **Step 3: Implement matrix.py**

Create `src/open_deep_research/factbase/matrix.py`:

```python
"""Build and render a cross-instance comparison matrix from grouped fact rows.

Input rows are query.group_by_canonical() output (one row per instance/property/
canonical value). Output: rows = instances, columns = the profile's properties,
cell = canonical value(s) with a trailing '*' for trusted, '!' for in-conflict;
empty string = no fact (a visible coverage gap).
"""
from __future__ import annotations

import csv
import io


def _cell_text(values: list[dict]) -> str:
    if not values:
        return ""
    parts = []
    for v in values:
        s = str(v.get("value", ""))
        if v.get("admission") == "trusted":
            s += "*"
        if v.get("in_conflict"):
            s += "!"
        parts.append(s)
    return "; ".join(sorted(parts))


def build_matrix(grouped_rows: list[dict], property_names: list[str], label) -> list[dict]:
    """label: callable instance_key -> display name. Returns rows sorted by display name."""
    by_instance: dict[str, dict[str, list[dict]]] = {}
    for r in grouped_rows:
        ik = r.get("instance_key")
        by_instance.setdefault(ik, {}).setdefault(r.get("property_name"), []).append(r)
    out = []
    for ik, props in by_instance.items():
        cells = {p: _cell_text(props.get(p, [])) for p in property_names}
        out.append({"instance_key": ik, "instance": label(ik), "cells": cells})
    out.sort(key=lambda row: row["instance"])
    return out


def render_matrix(grouped_rows: list[dict], property_names: list[str], label,
                  fmt: str = "text") -> str:
    matrix = build_matrix(grouped_rows, property_names, label)
    headers = ["country", *property_names]
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(headers)
        for row in matrix:
            w.writerow([row["instance"], *[row["cells"][p] for p in property_names]])
        return buf.getvalue().rstrip("\n")
    if fmt == "md":
        lines = ["| " + " | ".join(headers) + " |",
                 "| " + " | ".join("---" for _ in headers) + " |"]
        for row in matrix:
            lines.append("| " + " | ".join(
                [row["instance"], *[row["cells"][p] for p in property_names]]) + " |")
        return "\n".join(lines)
    # text: aligned columns
    widths = [len(h) for h in headers]
    for row in matrix:
        widths[0] = max(widths[0], len(row["instance"]))
        for i, p in enumerate(property_names, start=1):
            widths[i] = max(widths[i], len(row["cells"][p]))
    def fmt_row(cells):
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))
    lines = [fmt_row(headers)]
    for row in matrix:
        lines.append(fmt_row([row["instance"], *[row["cells"][p] for p in property_names]]))
    return "\n".join(lines)
```

- [ ] **Step 4: Run the test to confirm it passes**

Run: `uv run pytest tests/test_matrix_render.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/matrix.py tests/test_matrix_render.py
git commit -m "feat(factbase): cross-instance comparison matrix builder + renderer"
```

---

### Task 5: `dossier matrix` subcommand

**Files:**
- Modify: `src/open_deep_research/factbase/dossier.py`
- Test: `tests/test_dossier_matrix.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_dossier_matrix.py`. It seeds a temp DB through the real ingest path is heavy; instead seed the `fact` table minimally and call `run`. Use the existing schema bootstrap:

```python
import aiosqlite
import pytest

from open_deep_research import storage as _storage
from open_deep_research.factbase import migrations as _mig, schema as _schema
from open_deep_research.factbase.dossier import run


async def _seed(db_path):
    async with aiosqlite.connect(db_path) as conn:
        await _storage._ensure_schema(conn)
        await _mig.apply(conn, _schema.STEPS)
        await conn.execute(
            "INSERT INTO fact (instance_key, property_name, qualifiers_json, value, "
            "canonical_value, admission, lifecycle) VALUES "
            "('NGA','cbdc_launch_status','{}','launched','launched','trusted','current'),"
            "('IND','cbdc_launch_status','{}','pilot','pilot','provisional','current')")
        await conn.commit()


@pytest.mark.asyncio
async def test_matrix_subcommand_renders_rows(tmp_path):
    db = str(tmp_path / "m.db")
    await _seed(db)
    out = await run(["matrix", "--profile", "country_cbdc", "--format", "md"], db_path=db)
    assert "cbdc_launch_status" in out
    assert "Nigeria" in out and "India" in out
    assert "launched*" in out  # trusted marker
```

This requires the `country_cbdc` profile to exist (staged earlier in the session). If it is not committed, the test should `pytest.importorskip`-style skip; add at the top of the test:

```python
from importlib.resources import files
if not files("open_deep_research.factbase.profiles").joinpath("country_cbdc.yaml").is_file():
    pytest.skip("country_cbdc profile not present", allow_module_level=True)
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/test_dossier_matrix.py -q`
Expected: FAIL — `invalid choice: 'matrix'` from argparse.

- [ ] **Step 3: Add the subcommand parser**

In `src/open_deep_research/factbase/dossier.py`, inside `_parser()`, after the `compare` parser block, add:

```python
    mx = sub.add_parser("matrix", help="Cross-country matrix: rows=instances, cols=profile properties.")
    mx.add_argument("--profile", default="country_digital_identity",
                    help="Profile whose properties form the matrix columns.")
    mx.add_argument("--format", choices=["text", "md", "csv"], default="text")
```

- [ ] **Step 4: Add the command handler**

In `dossier.py` `run()`, after the `compare` handling (before the final fallthrough/return for show/compare), add a branch. Place it alongside the other `args.command ==` branches:

```python
    if args.command == "matrix":
        from .profile import load as _load_profile
        from .matrix import render_matrix
        from .query import FactQuery
        from .entities import CountryResolver
        prof = _load_profile(args.profile)
        property_names = [pd.name for pd in prof.properties]
        resolver = CountryResolver()
        async with aiosqlite.connect(db_path) as conn:
            await _storage._ensure_schema(conn)
            await _mig.apply(conn, _schema.STEPS)
            q = FactQuery(conn)
            rows = []
            for name in property_names:
                rows.extend(await q.compare_grouped(name))
        return render_matrix(rows, property_names, resolver.instance_name, fmt=args.format)
```

Ensure the imports it uses (`_storage`, `_mig`, `_schema`) are available in that scope. They are imported lazily in the `recompute` branch; add the same lazy import at the top of the `matrix` branch to be safe:

```python
        from open_deep_research import storage as _storage
        from open_deep_research.factbase import migrations as _mig, schema as _schema
```

- [ ] **Step 5: Run the test to confirm it passes**

Run: `uv run pytest tests/test_dossier_matrix.py -q`
Expected: PASS (or SKIP if `country_cbdc` absent — if skipped, create it: `cp /tmp/country_cbdc.yaml src/open_deep_research/factbase/profiles/` if available, else use `country_digital_identity` in the test).

- [ ] **Step 6: Full suite + lint**

Run: `uv run pytest tests/ -q`
Expected: PASS (all prior + new).
Run: `uv run ruff check src/`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/open_deep_research/factbase/dossier.py tests/test_dossier_matrix.py
git commit -m "feat(dossier): matrix subcommand renders cross-country comparison"
```

---

## Self-Review

**Spec coverage (Units 1, 2, 5):**
- Unit 2 (resolver rebuild, blocker, unresolved-not-silent) → Task 2. The "unresolved reported" half lands fully in 7b's batch summary; here `resolve` returns `None` and `instance_name` echoes unknown keys — the data substrate. ✓
- Unit 1 (country-list resolver: explicit/group; scout deferred) → Task 3, with scout explicitly deferred to 7b (model call lives there). ✓
- Unit 5 (matrix builder + renderer + `dossier matrix`) → Tasks 4–5. ✓
- US-2 (Bahamas resolves), US-6/US-7 partial (matrix render, group expansion) covered; US-1/US-3/US-4/US-5 are 7b (orchestration). ✓

**Placeholder scan:** No TBD/TODO; every code step has complete code. The one conditional ("if `country_cbdc` absent, skip") is a concrete guard, not a placeholder. ✓

**Type consistency:** `resolve(name)->str|None` and new `instance_name(key)->str` used consistently (matrix `label` callable = `resolver.instance_name`). Matrix row shape (`instance_key`/`property_name`/`value`/`admission`/`in_conflict`) matches `query.group_by_canonical` output exactly. `render_matrix(rows, property_names, label, fmt)` signature identical across matrix.py and the dossier handler. ✓

**Note for executor:** `pycountry` is author-time only (regenerating `iso3166.yaml`); do not import it at runtime. If `pycountry` is unavailable when regenerating, the committed `iso3166.yaml` already covers all cases — only re-run the generator to refresh.
