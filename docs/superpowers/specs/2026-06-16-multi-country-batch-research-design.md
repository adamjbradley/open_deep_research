# Design — Multi-Country Batch Research

- **Date:** 2026-06-16
- **Layer:** Feature Spec / Design
- **Status:** Draft — pending user approval, then `*.feedback` multi-agent review
- **Builds on:** `2026-06-16-profiles-as-data-design.md` (profiles + source registries as editable, validated, scaffoldable YAML; per-run provenance stamping; structural recompute). This feature is the natural consumer of that work: a profile defines *what* to extract for one country; this defines *how to run the same profile across many countries* and promote the results.
- **On approval (execution step 0):** save here, commit, then run the Codex/Claude/Gemini `*.feedback` review heartbeat. **No implementation code until the spec converges.**

## Context & problem

Today a research run targets **one** subject. The factbase already keys facts per **entity instance** (`instance_key`, e.g. `NGA`) and already exposes `dossier compare` (one property across all instances) — so the *storage* and *cross-country comparison* substrates exist. What is missing is the **orchestration** to run the same research (same profile, optional question template) across a list of countries so those instances get populated comparably in one job.

Two concrete blockers surfaced while validating the profiles-as-data work on a live eNaira run:

1. **The entity resolver is a hardcoded 20-country dict** (`factbase/entities.py`). Countries outside it — including the canonical CBDC case **Bahamas**, plus China, Jamaica, Ghana — resolve to `None`, and in `ingest.py` a `None` instance_key causes the fact to be **silently dropped**. A multi-country batch would quietly lose coverage. This is the same "hardcoded, needs code maintenance per entry" smell the profiles work removed for schemas.
2. **No source registry matched the CBDC domains**, so every extracted fact stalled at `admission=provisional` and nothing promoted to `trusted` — a comparison matrix of provisional-only values is weak. Promotion requires a registry that scores source domains by trust tier.

**Outcome:** a thin, resumable batch orchestration layer over the existing per-country graph that (a) resolves a country list three ways, (b) resolves *any* country name to an instance key from data, (c) auto-provisions a committed source registry so facts can promote, (d) runs each country as a normal `deep_researcher` invocation bounded-concurrently with a SQLite resume ledger, and (e) renders both per-country dossiers **and** a cross-country matrix.

## Locked decisions (from brainstorming)

1. **Deliverable = both**, equally: per-country dossiers *and* a cross-country comparison matrix from one batch run.
2. **Country list = three input modes** feeding one resolver: explicit list (`A,B,C` or `@file`), named group/region (`G20` → a `groups.yaml` data file), and scout discovery (LLM/search finds the relevant countries).
3. **Execution = bounded-concurrent + resumable**: run K countries at once (default K=3, reusing `max_concurrent_research_units` semantics); checkpoint each in a SQLite ledger; a re-run skips `done` items and retries `failed`/`pending`.
4. **Source registry = auto-scaffold, commit, use immediately** when missing, so facts promote in the same run. A non-blocking annotated `.draft.yaml` audit trail is still written, and tiering defaults conservatively (a domain earns `authoritative` only with explicit justification). No human gate (explicit owner choice).
5. **Architecture = thin batch driver (Approach A)**: orchestration wraps the existing single-country graph; the `deep_researcher` graph is untouched.

## Architecture — components

```
research_batch (CLI: `research-batch` / `dossier batch`)
├─ 1. CountryListResolver   input spec → [country names]   (explicit | group(DATA) | scout)
├─ 2. EntityResolver (REBUILT)  name → instance_key, full ISO-3166 (DATA); unresolved REPORTED
├─ 3. RegistryProvisioner   missing registry → scaffold + commit + use (live .yaml + audit .draft.yaml)
├─ 4. BatchRunner           run deep_researcher per country, bounded-concurrent
│      └─ BatchLedger (SQLite)  checkpoint per country; resume skips done, retries failed
└─ 5. MatrixRenderer        extend `dossier compare` → rows=countries × cols=properties (table|csv|md)
```

Five new independently-testable units + one rebuild (the resolver). Units 1–3 follow the profiles-as-data principle: behavior from editable, validated YAML (groups, the ISO country list, registries), not code.

## Data flow

```
research-batch --profile country_cbdc --countries "G20" --concurrency 3
  1. resolve list      G20 → [19 names]                        (CountryListResolver)
  2. ensure registry   no cbdc registry? → scaffold+commit       (RegistryProvisioner)
  3. resolve each name → instance_key   (ISO-3166 data; unresolved → reported, NOT dropped)
  4. for each country not 'done' in ledger (K concurrent):       (BatchRunner + BatchLedger)
        run deep_researcher(profile=country_cbdc, topic=template(country))
        success → facts ingest under instance_key, promote via registry, ledger 'done' (+run_id)
        failure → ledger 'failed' (+error), continue others
  5. all done → matrix across instances (table|csv|md) + ledger summary
               (done/failed/skipped, unresolved names, profile_hash drift if any)
```

## Components in detail

### Unit 1 — CountryListResolver
`resolve_country_list(spec) -> list[str]` with three strategies selected by the CLI flag used:
- **explicit:** `--countries "Nigeria,India,Bahamas"` or `--countries @path/to/list.txt` (one per line).
- **group:** `--countries G20` → looked up in `factbase/data/groups.yaml` (`G20: [...]`, `West Africa: [...]`, `EU: [...]`). A new data file, validated on load (non-empty lists, names resolvable). Editable without code change.
- **scout:** `--scout "countries that have launched a retail CBDC"` → one LLM/search call returning a candidate name list. Over/under-inclusion is mitigated by `--dry-run` (below). The scout returns **names only**; research happens in Unit 4.

### Unit 2 — EntityResolver (rebuild)
Replace the 20-entry `_ALPHA3` dict with an ISO-3166 lookup loaded from a bundled data file (`factbase/data/iso3166.yaml`, alpha-3 keyed), normalized identically (NFKD diacritic-fold + lowercase + strip non-alphanumerics) and extended with common aliases ("UK"→GBR, "South Korea"→KOR, "UAE"→ARE, "USA"→USA). Contract preserved: `CountryResolver.resolve(name) -> str | None`, so `ingest.py` and all callers are untouched. **Strict superset** of today (the original 20 still resolve). The behavioral change downstream: an **unresolved name is surfaced as a reported error**, never a silent fact drop — the batch summary lists "couldn't resolve: [...]", and `--dry-run` shows it pre-flight.

### Unit 3 — RegistryProvisioner
`ensure_registry(profile, *, db_path, autocommit) -> registry_name`:
1. If `Configuration.registry_name` loads to a non-empty registry, use it.
2. Else derive a name from the profile (`country_cbdc` → `cbdc_source_registry`). If that YAML exists, use it.
3. Else **scaffold** one: reuse the scaffolding machinery from the profiles work, targeting a **registry meta-schema** (`domain → tier → flags`) rather than a property profile. Seed the generator with the profile description (first pass). Write live `cbdc_source_registry.yaml` + audit `cbdc_source_registry.draft.yaml` (per-tier rationale + confidence). `git add` + commit both, then load and use immediately.
   - Conservative default: a domain earns `authoritative` only with explicit justification; otherwise `reputable`/`unvetted`.
4. **Optional enrichment (second pass, deferrable):** after the batch, the runs have captured the domains actually seen (`source.url_or_domain`); re-scaffold/extend the registry from those real domains and commit again. The first pass unblocks promotion; the enrichment improves tier accuracy. If kept to one pass at plan time, drop this step.

Effect: with tiers present, `promotion.evaluate` can lift corroborated facts `provisional → trusted`. `--no-registry-autoprovision` disables the whole behavior (facts stay provisional, no commits).

### Unit 4 — BatchRunner + BatchLedger
New SQLite tables in the same DB:
```
batch_run:   batch_id, profile_name, profile_hash, list_spec, created_at
batch_item:  batch_id, instance_key, country_name, status, run_id, error, updated_at
             status ∈ {pending, running, done, failed}
```
`batch_id` is derived deterministically from `(profile_name, normalized list spec)` so a re-run reattaches to the same ledger. BatchRunner iterates items, runs `deep_researcher` per country bounded to K concurrent (asyncio semaphore, mirroring `max_concurrent_research_units`), and updates the ledger transactionally per item. **Resume:** skip `done`, retry `failed`/`pending`. Idempotent re-run of a completed batch is a no-op (+ re-renders the matrix). `profile_hash` is recorded per batch so a mid-batch profile edit can be flagged (ties into existing drift machinery). Each per-country run is a normal single-country graph invocation — **the graph is not modified**.

### Unit 5 — MatrixRenderer
Extend `dossier compare` (today: one property across instances) into a full matrix: rows = countries (instances), columns = the profile's properties, cell = canonical value(s) with a `trusted`/`provisional` marker; empty cell = not found (visible coverage gap). `--format table|csv|markdown`. Exposed both inline at end of a batch and standalone via `dossier matrix --profile P`.

## CLI surface

```
research-batch --profile P --countries "G20"|"A,B,C"|@file  |  --scout "<query>"
               [--concurrency K=3] [--db PATH] [--format table|csv|markdown]
               [--no-registry-autoprovision] [--dry-run]
dossier matrix --profile P [--format table|csv|markdown]     # render anytime from stored facts
```
`--dry-run` resolves the list + reports what would run (and unresolved names) without spending tokens — a cheap pre-flight for large/scout batches.

## User stories (acceptance criteria)

- **US-1 batch run:** `research-batch --profile country_cbdc --countries "Nigeria,India,Bahamas"` produces three persisted dossiers + a 3-row matrix. *AC:* three `research_runs` rows (each stamped `profile_name/hash`), facts under `NGA`/`IND`/`BHS`, matrix populated.
- **US-2 resolver coverage:** `Bahamas` resolves to `BHS` and its facts persist (not dropped). *AC:* with the rebuilt resolver, a Bahamas run yields ≥1 stored fact; the original 20 still resolve.
- **US-3 unresolved reported:** an unknown/misspelled name is reported, never silently dropped. *AC:* `--dry-run` and the batch summary both list it under "couldn't resolve".
- **US-4 resumable:** killing a batch mid-way and re-running skips completed countries. *AC:* `done` items are not re-researched; `failed`/`pending` are retried; completed-batch re-run spends no research tokens.
- **US-5 promotion via auto-registry:** a profile with no matching registry gets one scaffolded+committed, and corroborated facts promote. *AC:* missing → `cbdc_source_registry.yaml` (+ `.draft.yaml`) committed; at least one fact transitions `provisional → trusted`.
- **US-6 matrix:** the comparison matrix renders with trusted markers and visible coverage gaps. *AC:* `dossier matrix --profile country_cbdc --format markdown` emits rows×properties with markers and empty cells where unknown.
- **US-7 list modes:** explicit, group, and scout all yield a runnable list. *AC:* `G20` expands from `groups.yaml`; `--scout` returns names; both feed the same runner.

## Required coverage

- **Safety & harm (epistemic):** The added power is breadth — one command now shapes facts for *many* countries at once, so a flawed profile or a mis-tiered auto-registry has amplified reach. Mitigations: conservative default tiering, the non-blocking `.draft.yaml` audit trail, `provisional`/`trusted` markers visible in the matrix, per-run `profile_hash` stamping, and `--dry-run`. The owner has explicitly accepted no human gate on registry adoption; the audit trail preserves after-the-fact reviewability.
- **Inclusion:** The resolver rebuild is itself an inclusion fix — today only 20 (mostly large) countries resolve; small states (Bahamas, Eastern Caribbean) are exactly the CBDC pioneers being dropped. ISO-3166 coverage + alias handling (endonyms/exonyms, "Türkiye"/"Turkey") widens representation. `groups.yaml` must avoid encoding only Western blocs.
- **Legal & compliance:** Profiles/registries/groups are schema + public-source trust data, not PII. Auto-committing registries creates a git provenance trail (who/when/what tiers) — defensible. The matrix may juxtapose politically sensitive country comparisons; values carry source provenance + trusted markers so figures are traceable, not asserted.
- **Risk & exploitation:** (a) **Scout over/under-inclusion** → `--dry-run` review before spend; scout returns names only, never tiers. (b) **Prompt-injection via scout/seed sources** into the registry generator → treat fetched text as data; generated registry must pass the registry meta-schema; conservative tiering caps damage. (c) **Auto-commit churn** → registries are committed files under normal code review after the fact; `--no-registry-autoprovision` is the escape hatch. (d) **Cost runaway** on a large batch → bounded concurrency + `--dry-run` cost preview + resumability so spend is never duplicated.
- **Erosion over time:** Failure mode = silent coverage gaps (dropped countries) or stale tiers. Addressed by unresolved-name reporting, matrix coverage gaps as first-class output, the ledger as an auditable record, and registry enrichment from observed domains.
- **Economic viability:** Batch is the multiplier that makes the per-domain profile investment pay off (one schema → N countries). Marginal cost is N research runs (bounded, resumable, dry-run-previewable) + one (or two) registry scaffolds per new domain — proportionate and controllable.
- **Unknown unknowns:** Surface in the `*.feedback` round — pressure-test (a) ISO alias edge cases / disputed territories, (b) ledger semantics under concurrent failure/partial writes, (c) whether one-pass vs two-pass registry provisioning gives trustworthy tiers, (d) matrix legibility when properties have qualifiers (retail vs wholesale CBDC) — does a flat matrix collapse meaningfully distinct facts?

## Critical files (seams)

- `factbase/entities.py` — rebuild `CountryResolver` to load ISO-3166 from data; **same signature**.
- `factbase/data/iso3166.yaml` *(new)* — alpha-3 country list + aliases.
- `factbase/data/groups.yaml` *(new)* — named groups/regions → country lists.
- `factbase/batch.py` *(new)* — `CountryListResolver`, `BatchRunner`, `BatchLedger`.
- `factbase/registry_provision.py` *(new)* — `ensure_registry()` + registry scaffolding (reuses `scaffold.py` machinery against the registry meta-schema).
- `factbase/schema.py` (`STEPS`) — migration adding `batch_run` + `batch_item` tables.
- `factbase/dossier.py` — `matrix` subcommand; `research-batch` entry (new console script or `dossier batch`).
- `factbase/compare`/render path — extend one-property compare into the full matrix renderer.
- `configuration.py` — batch defaults (`batch_concurrency`, `registry_autoprovision`) if surfaced as config.
- `tests/` — `test_entity_resolver_iso.py`, `test_batch_ledger.py`, `test_registry_provision.py`, `test_matrix_render.py`, `test_country_list_resolver.py`, `test_batch_end_to_end.py`.

## Verification

1. `uv run pytest` — existing suite green (resolver superset preserves the original 20; `ingest.py` untouched).
2. Resolver: ISO names + aliases resolve; unresolved returns `None` *and* is reported; Bahamas→BHS.
3. Ledger: resume skips `done`, retries `failed`; deterministic `batch_id`; completed re-run = no research calls.
4. Registry provision (temp DB): missing → scaffold + commit (live + draft) → observable `provisional → trusted` on a corroborated fact; `--no-registry-autoprovision` leaves facts provisional + no commit.
5. Matrix: rows×properties with trusted markers + empty coverage cells; csv/markdown formats.
6. List resolver: explicit/`@file`, group expansion from `groups.yaml`, scout returns names; `--dry-run` previews + lists unresolved without token spend.
7. End-to-end (mock model): 2-country batch persists both dossiers + populates the matrix + ledger shows both `done`.
8. Live smoke (subscription, opt-in): a 3-country CBDC batch via the `run-research-query` Path B style runner — dossiers persist, facts promote, matrix renders.

## Out of scope (deferred fast-follows)

- Multi-entity-type batches (only one `entity_type`/profile per batch for now).
- Parallelizing *within* a country beyond existing `max_concurrent_research_units`.
- A UI/Studio view for the matrix (CLI + CSV/markdown export first).
- Automatic scheduled re-runs / freshness policies per country.
- Qualifier-aware matrix pivots (e.g. separate retail vs wholesale columns) beyond a first flat matrix — noted as an open question.

## Open questions for the `*.feedback` round

- Registry provisioning **one-pass (description-seeded) vs two-pass (enriched from observed domains)** — accuracy vs simplicity.
- ISO-3166 list: **vendor a static data file vs depend on a library** (e.g. `pycountry`) — footprint vs maintenance.
- `batch_id` determinism: derive from `(profile, list spec)` vs an explicit user-supplied `--batch-id` for re-attaching across differing specs.
- Matrix legibility when properties carry qualifiers — flat matrix vs qualifier-aware pivot (currently deferred).
- Entry point: standalone `research-batch` console script vs `dossier batch` subcommand (consistency vs discoverability).
