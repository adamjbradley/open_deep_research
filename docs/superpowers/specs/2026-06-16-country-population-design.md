# Design — Country Population (numeric fact + direct dataset load)

- **Date:** 2026-06-16
- **Layer:** Feature Spec / Design
- **Status:** Draft — approved in brainstorming; ready for plan.
- **Builds on:** the factbase (`profiles-as-data`, ISO-3166 resolver, source registries, Ingestor/promotion) and the multi-country batch work. This adds the first **numeric** fact and the first **direct dataset loader** (no LLM research).

## Context & problem

The factbase captures researched, synthesized facts (CBDC status, digital-identity schemes) via the LLM research graph. The owner wants a single, simple, well-known fact — **population** — for **all countries**. Two realities shape the design:

1. **Population is reference data**, not a research target. An authoritative dataset (World Bank `SP.POP.TOTL`) has the exact number; running the research graph ~195–249 times to retrieve a known integer would be slow, costly, and *less* accurate than the dataset. So population is loaded **directly** into the factbase, bypassing the research graph but reusing the ingest/promotion/provenance machinery.
2. **The factbase has no numeric value-kind.** Kinds are `name / enum / percentage / boolean / name_year`. Population is a count, so we add a first-class **`number`** kind (parallel to `percentage`) rather than storing it as a loose `name` string.

**Outcome:** a `country_population` profile with a `population: number` property, a vendored World Bank dataset, and a `dossier population-load` command that ingests one most-recent population per country — landing as `trusted` from an authoritative World Bank source — viewable via `dossier matrix --profile country_population`.

## Locked decisions (from brainstorming)
1. **Populate method = direct dataset load** (not batch LLM research).
2. **Value typing = add a numeric `number` kind** to the factbase (not store-as-string).
3. **Data source = vendored, generated `data/population.yaml`** from World Bank `SP.POP.TOTL`, via an author-time script (mirrors `data/iso3166.yaml`). No runtime network.
4. **Loader reuses the existing `Ingestor`** (canonicalization, source-tiering, conflict, promotion) and lands as a `dossier population-load` subcommand.

## Components

### 1. `number` value-kind (factbase core)
Three seams, parallel to `percentage`:
- `factbase/profile_schema.py` — add `"number"` to `_VALID_KINDS`.
- `factbase/profile.py` `PropertyDef.validate` — `number` branch: strip group separators (`,`, `_`, spaces), `float()`-parse; return True on a finite number, False otherwise.
- `factbase/identity.py` `canonical_value` — `number` branch: strip separators, parse, emit a canonical numeric string (integral values as plain digits, e.g. `"1,402,000,000"` and `"1402000000"` → `"1402000000"`); preserves non-integral as a normalized float string. **Out of scope:** multiplier words ("1.4 billion") — the loader supplies clean integers; flagged as a future enhancement for research-extracted numbers.

### 2. `country_population` profile
`factbase/profiles/country_population.yaml`:
```yaml
entity_type: country
version: '1'
properties:
- name: population
  kind: number
  description: Total population (most recent World Bank SP.POP.TOTL estimate).
  trust_threshold: reputable
```
No qualifiers. The time dimension is the per-fact `as_of` (year). v1 stores **one** most-recent value per country (no timeseries); a later re-load of a newer vintage supersedes via the existing recompute/conflict path.

### 3. Vendored data + author-time generator
- `scripts/gen_population.py` (author-time only; not imported at runtime): fetch `https://api.worldbank.org/v2/country/all/indicator/SP.POP.TOTL?format=json&mrnev=1&per_page=400` via stdlib `urllib.request`, keep ISO-3166 alpha-3 entries (skip aggregates), write `factbase/data/population.yaml` → `{ALPHA3: {value: int, year: int}}`, sorted. Dependency-free.
- `factbase/data/population.yaml` (committed, generated).

### 4. Source registry
`factbase/profiles/country_population_source_registry.yaml` — minimal:
```yaml
version: '1'
sources:
- {domain: data.worldbank.org, tier: authoritative, flags: [primary]}
- {domain: api.worldbank.org, tier: authoritative, flags: [primary]}
- {domain: worldbank.org, tier: authoritative, flags: []}
```
So each fact's single World Bank source clears the promotion bar → `trusted` (same mechanism that promoted CBDC facts from `bis.org`).

### 5. Loader
`factbase/population_loader.py` — `load_population(conn, *, profile, registry, source_year_url, data) -> dict`:
- `data` (default: read `data/population.yaml`) → for each `ALPHA3 -> {value, year}`, build a record:
  `{"property": "population", "instance_name": CountryResolver().instance_name(alpha3), "value": str(value), "as_of": year, "source_url": "https://data.worldbank.org/indicator/SP.POP.TOTL", "evidence_span": "World Bank SP.POP.TOTL most-recent estimate"}`.
- Preallocate a run (`storage.preallocate_run` or the minimal run row the Ingestor needs), call `Ingestor(conn, profile=prof, resolver=CountryResolver(), registry=reg).ingest(run_id=..., records=...)`.
- Return `{"loaded": n, "trusted": m, "instances": k}`.

`instance_name` is the ISO primary name; the Ingestor's resolver maps it back to the same alpha-3 (round-trips cleanly). Records whose alpha-3 isn't a resolvable country are skipped + reported (consistent with the resolver's no-silent-drop contract).

### 6. Entry point
`dossier population-load [--db PATH]` in `factbase/dossier.py` — runs the loader against the resolved DB, prints `loaded N (M trusted) across K countries`. Viewing reuses `dossier matrix --profile country_population`, `dossier compare population`, `dossier show <country>`.

## Data flow
```
gen_population.py (author-time) --[World Bank API]--> data/population.yaml (committed)
dossier population-load --> read population.yaml --> records --> Ingestor.ingest
   (CountryResolver maps names->alpha3; canonical_value[number]; registry tiers worldbank
    authoritative; promotion -> trusted) --> facts in SQLite
dossier matrix --profile country_population --> population column across all countries
```

## User stories (acceptance criteria)
- **US-1 numeric kind:** a profile may declare `kind: number`; `dossier validate` accepts it. *AC:* meta-schema accepts; a typo'd kind still rejects.
- **US-2 numeric validation/canonicalization:** `"1,402,000,000"` validates and canonicalizes equal to `"1402000000"`; `"abc"` fails validation. *AC:* unit tests on `validate` + `canonical_value`.
- **US-3 load:** `dossier population-load` ingests one population per country from the vendored data. *AC:* facts present for the loaded countries, `property=population`, `as_of` = year.
- **US-4 promotion:** loaded population facts land `trusted` via the World Bank registry. *AC:* ≥1 fact `admission='trusted'` with source `data.worldbank.org [authoritative]`.
- **US-5 view:** `dossier matrix --profile country_population` shows the population column across countries. *AC:* matrix renders with values + trusted markers.
- **US-6 coverage/no silent drop:** any alpha-3 not resolvable is reported, not dropped. *AC:* loader return/report lists skipped codes.

## Required coverage
- **Safety & harm:** Minimal — population is public reference data, not PII, no crisis surface. Epistemic care: values are stamped with `as_of` year + World Bank provenance so a figure is always traceable to a vintage; no synthesized/estimated numbers.
- **Inclusion:** Loading **all** ISO-3166 countries (not a subset) is itself inclusive; small states are covered identically. The World Bank set omits a few territories — report coverage gaps rather than hide them.
- **Legal & compliance:** World Bank data is openly licensed (CC BY 4.0); attribution is carried in `source_url`/`evidence_span`. Not PII.
- **Risk & exploitation:** The author-time fetch hits a public API; the generated YAML is reviewed in the diff before commit. The loader writes only validated numbers under one property. No untrusted input path.
- **Erosion over time:** Population drifts yearly. `as_of` + re-runnable generator keep it refreshable; a re-load of a newer vintage supersedes via the existing conflict/recompute path. Document that population.yaml is a snapshot with a generation date.
- **Economic viability:** ~zero marginal cost (one API fetch at author time; instant load) vs. ~hundreds of LLM runs for the research path — the entire point of choosing direct load.
- **Unknown unknowns:** number canonicalization edge cases (separators, locale, floats), World Bank aggregate/region codes leaking into the country set, and as_of/identity interaction (one value per country) — covered by tests + the country-only filter.

## Critical files (seams)
- `factbase/profile_schema.py` — `_VALID_KINDS` += `number`.
- `factbase/profile.py` — `PropertyDef.validate` number branch.
- `factbase/identity.py` — `canonical_value` number branch.
- `factbase/profiles/country_population.yaml` *(new)*.
- `factbase/profiles/country_population_source_registry.yaml` *(new)*.
- `scripts/gen_population.py` *(new, author-time)* + `factbase/data/population.yaml` *(generated)*.
- `factbase/population_loader.py` *(new)* — `load_population`.
- `factbase/dossier.py` — `population-load` subcommand.
- `tests/` — `test_factbase_number_kind.py`, `test_population_loader.py` (+ matrix render of population).

## Verification
1. `uv run pytest` — number-kind accept/reject + canonical collapse; loader ingests a fixture into a temp DB and asserts ≥1 `trusted` fact with World Bank provenance; coverage/skip reporting.
2. `uv run dossier validate` — `country_population.yaml` + its registry OK.
3. Generate `population.yaml` (author-time), spot-check a few known values (e.g. India ~1.4B, Nauru small), confirm only country alpha-3 codes (no aggregates).
4. `dossier population-load` into a temp DB → `dossier matrix --profile country_population` shows the column with trusted markers.

## Out of scope (deferred)
- Population **timeseries** (multiple years per country) — v1 is one most-recent value.
- Multiplier-word numeric parsing ("1.4 billion") for research-extracted numbers.
- A generic numeric **unit** system (per-capita, density) — just a raw count now.
- Auto-refresh scheduling of the dataset.
