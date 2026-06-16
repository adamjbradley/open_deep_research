# Design — Profiles as Data + Assisted Scaffolding

- **Date:** 2026-06-16
- **Layer:** Feature Spec / Design
- **Status:** Revised after `*.feedback` round 1 (agy ADVANCE, gemini ADVANCE, claude YES-with-conditions, codex HOLD). Blockers folded in below. Pending re-review, then an implementation plan. **No implementation code until the spec converges.**
- **Builds on:** Vision `2026-06-12-living-dossier-platform-design.md` P2 ("domain assumptions live *only* in the profile (data), never hard-coded… inspectable and revisable at one seam"); Architecture `2026-06-12-living-dossier-platform-architecture.md` §3 (profile/SourceRegistry ports, `registry_version`), §6 (recompute); Features `2026-06-12-living-dossier-platform-features.md` §2.1.

## Context & problem

The factbase is "domain-adaptable via a domain profile," and Vision **P2** commits that domain assumptions live in *data* at one inspectable seam. Reality: profiles are Python modules loaded by `importlib` (`factbase/profile.py:59-61`), so **every new query type needs code + redeploy**, domain experts can't author/audit without Python, and the bias seam isn't a data seam. The owner's pain is two-sided: (A) the **code/redeploy** burden, and (B) **authoring a schema from a blank file** per domain. Separately, the extraction prompt serializes only property *names* (`deep_researcher.py:1295`) — the model never sees enum vocabularies or guidance, weakening extraction.

**Outcome:** profiles become editable, validated, runtime-loaded YAML whose schema *compiles into* the extraction prompt and is *version-stamped per run*; **runtime selection** lets a run choose which profile drives it; and a **scaffolding assist** drafts a candidate profile so nobody starts from a blank file — human-gated.

## Locked decisions (brainstorm)
1. **Scope = A+B:** externalize profiles to data **and** add LLM-assisted scaffolding (human-gated).
2. **Scaffold placement = both:** offline `scaffold` primary; optional inline "no matching profile → offer to draft" prompt.
3. **Offline scaffold input = description-first, optional seed sources** (one shared induce routine; inline path feeds the run's sources).
4. **Granularity = one focused profile per domain/pillar, reusing the entity type** (`country_digital_identity.yaml`, later `country_cbdc.yaml`; both reuse the `country` entity/resolver).
5. **Generator review surface = full draft, risk-annotated** (rationale + confidence as inert YAML comments).

## Resolved open questions (from feedback round 1 — consensus)
- **Meta-schema = Pydantic-in-code** (all four reviewers). No second schema language; located errors; experts edit profiles, not the meta-schema.
- **Versioning = both, with mismatch detection.** Machine `profile_hash` (sha256 of canonical bytes) is the **integrity/reproducibility key** stamped on runs; author-managed `version:` is a human changelog. Loading a profile whose `version:` is unchanged but whose `profile_hash` differs from the last run's stamp is the un-versioned-edit erosion case → **warn loudly** (don't just stamp).
- **Registry = externalize in this spec, but with its own rules.** `di_source_registry` (already a plain dict) moves to YAML, but it needs a *dedicated* meta-schema (domain match, tier vocabulary, flags, per-property trust bars), its own `registry_version`/hash, and its own recompute impact on `source.tier`/`source.flags_json`/promotion — not just "the same way" as profiles.

## Pillar 1 — Profiles as data

### Data-file format (YAML, `factbase/profiles/`)
1:1 with today's `Profile`/`PropertyDef` — **including `value_aliases`** (present in `profile.py:19`, used by the live Aadhaar map; omitting it makes the migration lossy) — plus `description` per property, optional per-enum-value descriptions, and a `version:`. **`load(name) -> Profile` / `SourceRegistry.load(name)` signatures stay identical** (read+parse YAML instead of importing), so all 21 factbase tests and call sites are untouched; `PropertyDef.validate()/aliases_for()/property()` unchanged. **Golden round-trip test:** `load(".py" PROFILE) == load(".yaml")` for the real profile, proving lossless migration. **Draft files are written/read as raw annotated text — never parse→re-dump** (that would strip the review comments).

### Selection contract (NEW — blocker #1)
The engine currently hardcodes `load("country_digital_identity")` (`deep_researcher.py:350`, `:1383`), so multiple profiles are unreachable and US-2 / the inline offer have nothing to resolve against. v1 contract:
- Add `Configuration.profile_name` (default `country_digital_identity`) and `Configuration.registry_name` (default `di_source_registry`). Replace both hardcoded `load(...)` sites with `load(configurable.profile_name)` / `SourceRegistry.load(configurable.registry_name)`, and thread the selected names through facts-first target-property resolution, extraction, sufficiency, rendering, and persistence.
- **Inline offer** keys off this: if the selected `profile_name` doesn't resolve to a file, surface "no profile for X — run `dossier scaffold …`?" — never auto-adopt.
- **Deferred:** automatic *brief→entity_type/pillar→profile* resolution (a `subject`/pillar resolver). v1 is explicit selection via config; the resolver is a named future seam.
- **Acceptance test:** set `profile_name=country_cbdc`, run → it drives extraction with **no Python edited**.

### Validate-on-load (Pydantic meta-schema)
Validates each profile on load *and* via a new `dossier validate` CLI (CI-gated). Rejects: unknown `kind`; `value_enum` on a non-enum kind; a `qualifier_enums` key absent from the qualifier lists; `required_qualifiers ⊄ identity_qualifiers`; duplicate property/enum values; empty `entity_type`; and **`value_aliases` whose canonical keys are invalid for the kind or overlap across canonicals**. Errors name file + property + problem. The registry gets its own meta-schema (per Resolved-questions).

### Prompt compilation (the one behavior-affecting change)
Replace the names-only body in `_make_fact_model_call` (`deep_researcher.py:1284-1319`) with one rendered from the profile — per *target* property: `name (kind) — description; allowed values [...]; qualifiers {...}`. Facts-first (#19) already restricts to `target_properties`, bounding tokens; still, the compiler **warns if a compiled profile exceeds a token budget** (agy prompt-bloat). Keep existing guardrails (verbatim `evidence_span`, omit-if-unstated, empty-if-nothing). Gate behind a config flag defaulting **on**, with the names-only path retained as the measured baseline (see Verification A/B).

### Versioning, provenance & recompute (blocker #2)
- Stamp `profile_name`, `profile_version`, `profile_hash` on `research_runs` via a new migration (schema is at **v5** → add **v6**). Because `preallocate_run()` runs *before* a profile is selected (`storage.py:316-328`), **stamp after selection/load and before extraction** by updating the preallocated row; finalize preserves it and **fails visibly if extraction ran unstamped**.
- **Recompute is classified by edit type** (canonical-only recompute is insufficient for structural edits):
  - *Normalization edits* (`value_aliases`, canonicalization): `recompute.backfill_canonical_values(force=True)`.
  - *Structural edits* (`identity_qualifiers`, `required_qualifiers`, `value_enum`, `qualifier_enums`, `trust_threshold`, property add/rename/remove): a **forward-only rebuild** that recomputes tuple keys, re-runs conflict grouping, re-evaluates promotion, and refreshes read models from retained `fact`/`evidence` rows.
  - **Trigger:** on `load()`, if the current `profile_hash` ≠ the latest run's stamped hash for that profile, **warn and require an explicit `dossier recompute [--rebuild] <profile>`** — do not silently rebuild mid-run. (Otherwise the erosion the spec claims to fix persists.)

### Packaging & dependency (codex Medium)
- Add `*.yaml` under `open_deep_research.factbase.profiles` to `[tool.setuptools.package-data]` (today only `py.typed` ships) + an **installed-wheel smoke test** that loads the shipped profile and registry.
- Declare **`pyyaml` as a direct dependency** (`yaml.safe_load`); it is only transitive today.

## Pillar 2 — Assisted scaffolding (human-gated)

- **Offline:** `dossier scaffold <entity_type> "<description>" [--seed URL ...] [--out path]`.
  1. If `entity_type` exists, **reuse** its identity/resolver; generate only this pillar's new properties.
  2. Context = description (always) + `--seed` source text (optional grounding) via the existing fetch path. The system prompt **explicitly directs the model toward localized/non-Western schema structures** (avoid Anglo-default vocabularies — agy/claude inclusion).
  3. Propose properties (name, kind, enums, qualifiers, descriptions, `value_aliases`). For each **identity-qualifier** and **enum** decision, emit a YAML comment with rationale + a `confidence` flag.
  4. **Validate the draft against the meta-schema** before writing — invalid drafts can't be saved.
  5. Write the annotated candidate to `<name>.draft.yaml`. Human edits, drops `.draft`, commits. Only committed, validating profiles are loadable.
- **Inline offer:** see Selection contract — surfaces a suggestion, never auto-adopts.
- **Shared routine:** `factbase/scaffold.py:induce(description, sources, existing_entity_type) -> draft_yaml`.
- **Injection hardening (claude #5/#6):** seed text is data, not instructions; the generator only emits schema that must pass the meta-schema. The **rationale/confidence comments are themselves a social-engineering surface** (crafted seed text → persuasive rationale that nudges the reviewer) — keep them inert, clearly machine-generated, and never let them suppress meta-schema validation.

## Data flow
```
scaffold:  description (+seed sources) ─▶ induce() ─▶ candidate .draft.yaml (risk-annotated, raw text)
                                                   └─▶ meta-schema validate ─▶ human edit/approve/commit
research:  brief ─▶ select profile_name (config) ─▶ load+validate ─▶ stamp run ─▶ compile prompt ─▶ extract ─▶ ingest
edit a profile ─▶ bump version ─▶ (hash mismatch on next load → warn) ─▶ dossier recompute [--rebuild]
```

## User stories (acceptance criteria)
- **US-1 author:** edit a YAML enum, no redeploy; next run admits the new value. *AC:* change `value_enum`, run, new value accepted; no Python edited.
- **US-2 new pillar:** `dossier scaffold country "CBDC…"` → reviewed `country_cbdc.yaml` → run with `profile_name=country_cbdc` drives extraction. *AC:* a second profile drives a run **with no engine code change**.
- **US-3 safety:** malformed profile fails on load and in `dossier validate` CI. *AC:* seeded bad profile → non-zero exit + located message.
- **US-4 extraction:** model receives enum vocabularies + descriptions. *AC:* rendered prompt for `scheme_status` lists its values + descriptions; measured A/B shows no net increase in false/unsupported facts (below).
- **US-5 assist:** scaffolding yields a non-blank, risk-annotated, meta-schema-valid draft with identity/enum rationale + confidence.
- **US-6 provenance & recompute:** runs carry `profile_name/version/hash`; an edit + `dossier recompute` propagates to prior facts; a same-`version`/different-hash load warns.

## Required coverage
- **Safety & harm:** no self-harm/crisis surface. Harm is *epistemic* — a biased/incomplete (or generated) schema shapes what "facts" exist. Mitigated by the human gate, inspectable diffable YAML, risk-annotations, meta-schema validation, version+hash stamping with mismatch warnings. Net gain over hidden-in-code assumptions.
- **Inclusion:** enum vocabularies encode a worldview; readable YAML + descriptions + the generator's localized-structure directive make them contestable/extensible (e.g., non-Western ID regimes).
- **Legal & compliance:** public-policy/identity domain (Aadhaar; `data_protection_law`). `profile_hash` + git history give a defensible provenance chain. Profiles are schema, not PII.
- **Risk & exploitation:** (a) prompt-injection via seed sources → seed text is data; output must pass the meta-schema; (b) automation bias → risk-annotations + the rationale-comment-as-injection-surface caveat; (c) profiles are repo files under code review, never runtime user-supplied. Fully-autonomous (no-gate) induction stays out of scope precisely because it removes the gate.
- **Erosion over time:** silent schema drift / un-versioned edits → version+hash per run, **mismatch warning**, classified recompute, CI validation; per-pillar files keep review bounded.
- **Economic viability:** removes per-domain engineering cost and lowers authoring cost (scaffold drafts the file). Added cost: a few prompt tokens/extraction + one LLM call per *new domain* scaffold — negligible.
- **Unknown unknowns:** surfaced by round 1 — selection contract (#1) and recompute scope/trigger (#2) were the two the draft under-specified; both now addressed.

## Critical files (seams)
- `factbase/profile.py` — YAML `load()` + Pydantic meta-schema; `description` on `PropertyDef`; preserve `value_aliases`. Dataclasses/methods otherwise unchanged.
- `factbase/registry.py` — YAML `SourceRegistry.load()` + its own meta-schema/version rules.
- `factbase/profiles/*.yaml` — `country_digital_identity.yaml`, `di_source_registry.yaml` (replace `.py`).
- `factbase/scaffold.py` *(new)* — `induce()`.
- `configuration.py` — `profile_name`, `registry_name` (+ prompt-compile flag).
- `deep_researcher.py` — selection at `:350`/`:1383`; prompt compilation at `:1284-1319`.
- `factbase/schema.py` (`STEPS` v6) + `storage.py` — migration + post-selection stamping; recompute classification in `factbase/recompute.py`.
- `factbase/dossier.py` — `validate`, `scaffold`, `recompute` subcommands.
- `pyproject.toml` — `pyyaml` dependency + `*.yaml` package-data.
- `tests/` — add `test_factbase_profile_schema.py` (meta-schema accept/reject), `test_factbase_profile_roundtrip.py` (golden .py==.yaml), `test_factbase_selection.py` (`country_cbdc` drives a run), `test_factbase_scaffold.py`; CI runs `dossier validate` + an installed-wheel smoke test.

## Verification
1. `uv run pytest` — existing factbase tests green (interfaces preserved).
2. Golden round-trip: `load(.py) == load(.yaml)` for the real profile (lossless, incl. `value_aliases`).
3. Meta-schema: valid loads; each malformed case (incl. bad `value_aliases`) raises a located error; `dossier validate` non-zero on a seeded bad profile, zero on real ones; wired into CI.
4. Selection: a run with `profile_name=country_cbdc` extracts under that profile, no Python edited.
5. **Prompt A/B on the standing India brief:** names-only vs compiled — report facts captured **and** false/unsupported facts; the compiled path must not increase the latter.
6. Provenance & recompute: run stamps `profile_name/version/hash` after selection; same-version/different-hash load warns; `dossier recompute --rebuild` updates tuple keys/conflicts/promotion after a structural edit.
7. Packaging: installed-wheel smoke test loads shipped profile + registry.
8. Scaffold: `dossier scaffold` yields a meta-schema-valid, risk-annotated draft from a description (and again with `--seed`).
9. Real run via the `run-research-query` skill (Path B, Tavily wired) on a country brief — facts ingest under the YAML profile, stamped.

## Suggested implementation order (for the plan stage)
1. **Plan 6a — Pillar 1 core:** YAML loader + `value_aliases` + golden round-trip + Pydantic meta-schema + `dossier validate` + selection contract (config field) + packaging/dependency. Lowest risk, unblocks everything.
2. Prompt compilation + flag + the A/B.
3. Provenance migration (v6) + post-selection stamping + classified recompute + `dossier recompute`.
4. **Plan 6b — Pillar 2 scaffolding:** `induce()`, `scaffold` CLI, inline offer, injection hardening.

Pillars 1 and 2 split into two implementation plans.

## Out of scope (deferred fast-follows)
- Fully **autonomous** induction (no human gate) + registry reconciliation/promotion of schema elements.
- Automatic **brief→profile resolution** (subject/pillar resolver) — v1 selects via config.
- Profiles as **DB rows** / editing API/UI.
- **Composable** multi-profile merging per entity type.
- Multi-tenant / runtime user-supplied profiles.

## Feedback round 1 (2026-06-16) — disposition
agy (ADVANCE) and gemini (ADVANCE) endorsed; claude (YES-with-conditions) and codex (HOLD) blocked on the same gaps, now folded in: **selection contract** (#1), **classified recompute + trigger** (#2), **`value_aliases` in format/meta-schema + golden round-trip** (#3), plus prompt-A/B, packaging/`pyyaml`, post-selection stamping, registry-own-schema, scaffold injection hardening, and localized-structure prompting. Open questions resolved (Pydantic meta-schema; both version+hash with mismatch warning; registry externalized with its own rules). Re-review recommended before advancing to the plan.
