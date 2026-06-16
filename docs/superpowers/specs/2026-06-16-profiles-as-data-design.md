# Design — Profiles as Data + Assisted Scaffolding

- **Date:** 2026-06-16
- **Layer:** Feature Spec / Design
- **Status:** Design approved (collaborative brainstorm). Pending `*.feedback` multi-agent review (Codex/Claude/Gemini) before transitioning to an implementation plan. **No implementation code until the spec converges.**
- **Builds on:** Vision `2026-06-12-living-dossier-platform-design.md` P2 ("domain assumptions live *only* in the profile (data), never hard-coded… inspectable and revisable at one seam"); Architecture `2026-06-12-living-dossier-platform-architecture.md` §3 (profile/SourceRegistry ports, `registry_version`), §6 (recompute); Features `2026-06-12-living-dossier-platform-features.md` §2.1.

## Context & problem

The factbase is "domain-adaptable via a domain profile," and Vision **P2** commits that domain assumptions live in *data* at one inspectable seam. Reality: profiles are Python modules loaded by `importlib` (`factbase/profile.py:59-61`), so **every new query type needs code + redeploy**, domain experts can't author/audit without Python, and the bias seam isn't a data seam. The owner's pain is two-sided (confirmed in brainstorming): (A) the **code/redeploy** burden, and (B) **authoring a schema from a blank file** for each new domain. Separately, the extraction prompt serializes only property *names* (`deep_researcher.py:1295`) — the model never sees enum vocabularies or any guidance, so extraction is weaker than it should be.

**Outcome:** profiles become editable, validated, runtime-loaded YAML whose schema *compiles into* the extraction prompt and is *version-stamped per run*; and a **scaffolding assist** drafts a candidate profile so nobody starts from a blank file — with a human as the approval gate, which keeps schema-drift risk controlled without needing autonomous induction yet.

## Locked decisions (from brainstorming)
1. **Scope = A+B:** externalize profiles to data **and** add LLM-assisted scaffolding (human-gated).
2. **Scaffold placement = both:** offline `scaffold` command is primary; an optional inline "no matching profile → offer to draft one" prompt during a run.
3. **Offline scaffold input = description-first, optional seed sources** (one shared "induce from sources" routine; inline path feeds it the run's sources).
4. **Granularity = one focused profile per domain/pillar, reusing the entity type** (e.g. `country_digital_identity.yaml`, later `country_cbdc.yaml`; both reuse the `country` entity/resolver).
5. **Generator review surface = full draft, risk-annotated:** propose everything, but annotate the consequential calls (which qualifiers are *identity*; enum completeness) with rationale + confidence as inert YAML comments, so review is targeted, not rubber-stamp.

## Pillar 1 — Profiles as data

- **Format:** YAML files in `factbase/profiles/`, 1:1 with today's `Profile`/`PropertyDef`, plus a `description` on each property and (optionally) per-enum-value descriptions, and a `version:` field. **`load(name) -> Profile` and `SourceRegistry.load(name)` signatures stay identical** — the loaders read+parse YAML instead of importing a module, so all 21 factbase tests and every call site are untouched. `PropertyDef.validate()/aliases_for()/property()` unchanged. `di_source_registry` (already a plain data dict) moves to YAML the same way.
- **Validate-on-load (the safety net that replaces compile-time checks):** a meta-schema (Pydantic model) validates each profile on load *and* via a new `dossier validate` CLI subcommand wired into CI. Rejects: unknown `kind`; `value_enum` on a non-enum kind; a `qualifier_enums` key absent from the qualifier lists; `required_qualifiers ⊄ identity_qualifiers`; duplicate property/enum values; empty `entity_type`. Errors name file + property + problem.
- **Prompt compilation:** replace the names-only prompt body in `_make_fact_model_call` (`deep_researcher.py:1284-1319`) with one rendered from the profile — per target property: `name (kind) — description; allowed values [...]; qualifiers {...}` — keeping the existing guardrails (verbatim `evidence_span`, omit-if-unstated, empty-if-nothing). This is the only **behavior-affecting** change; gate behind a config flag defaulting **on**, capture before/after extraction snapshots in review.
- **Versioning & provenance:** YAML carries `version:`; loader computes `profile_hash` = sha256 of canonical bytes. New migration step (after current v5 in `schema.py:STEPS`) adds `profile_version`, `profile_hash` to `research_runs` (mirrors the existing unused `source.registry_version`); stamp at `preallocate_run()`/finalize. On a profile edit: bump `version`; `recompute.backfill_canonical_values(conn, profile, force=True)` re-derives canonical values for prior facts; same version+hash ⇒ reproducible.

## Pillar 2 — Assisted scaffolding (human-gated)

- **Offline command:** `dossier scaffold <entity_type> "<domain description>" [--seed URL ...] [--out path]`.
  1. If `entity_type` already exists, **reuse** its identity/resolver and tell the generator to propose only this pillar's new properties.
  2. Build context: the description (always) + text of any `--seed` sources (optional grounding) via the existing fetch path.
  3. LLM proposes a profile: properties (name, kind, enums, qualifiers, descriptions). For each **identity-qualifier** and **enum** decision it emits a YAML comment with rationale + a `confidence` flag (the "flashlight on the traps").
  4. **Validate the draft against the meta-schema** before writing — a draft that can't pass can't be saved.
  5. Write the annotated candidate YAML to `--out` (default `factbase/profiles/<name>.draft.yaml`). The human edits, removes the `.draft` suffix, commits. Only committed, validating profiles are loadable by `load(name)`.
- **Inline offer:** when a run's brief resolves to an `entity_type`/pillar with no matching profile, surface "no profile for X — run `dossier scaffold …`?" (and, if sources were already fetched, offer to seed the draft from them). It **never auto-adopts** a generated profile into a live run — the gate holds.
- **Shared routine:** offline and inline both call one `factbase/scaffold.py:induce(description, sources, existing_entity_type) -> draft_yaml`.

## Data flow

```
scaffold:  description (+seed sources) ─▶ induce() ─▶ candidate .draft.yaml (risk-annotated)
                                                   └─▶ meta-schema validate ─▶ human edit/approve/commit
research:  brief ─▶ load(profile name) ─▶ validate-on-load ─▶ compile prompt ─▶ extract ─▶ ingest
                                                                                   └─▶ stamp profile_version/hash on run
edit a profile ─▶ bump version ─▶ recompute(force) re-derives canonical values for prior facts
```

## User stories (acceptance criteria)
- **US-1 author:** edit a YAML enum, no redeploy; next run admits the new value. *AC:* change `value_enum`, run extraction, new value accepted; no Python edited.
- **US-2 new pillar:** `dossier scaffold country "CBDC status…"` → reviewed `country_cbdc.yaml` → `load("country_cbdc")` drives a run. *AC:* a second profile loads and extracts, with no engine code change.
- **US-3 safety:** a malformed profile (typo'd kind, dangling qualifier) fails on load and fails `dossier validate` in CI — never reaches a run. *AC:* seeded bad profile → non-zero exit + clear message.
- **US-4 extraction:** the model receives enum vocabularies + descriptions. *AC:* rendered prompt for `scheme_status` lists its four values + descriptions; enum selection more consistent than the names-only baseline.
- **US-5 assist:** scaffolding a new domain produces a non-blank, risk-annotated draft. *AC:* `scaffold` emits a meta-schema-valid YAML with identity-qualifier/enum comments carrying rationale + confidence.
- **US-6 provenance:** every run records `profile_version`/`profile_hash`; recompute propagates a profile edit to prior facts. *AC:* run rows carry the stamp; `force` recompute updates canonical values/dedup.

## Required coverage
- **Safety & harm:** No self-harm/crisis surface. The harm is *epistemic* — a biased/incomplete (or generated) schema shapes what "facts" exist. Mitigated by: human approval gate on every profile, inspectable diffable YAML, risk-annotations that fight rubber-stamping, meta-schema validation, and per-run version stamping. Net safety gain over hidden-in-code assumptions.
- **Inclusion:** Enum vocabularies encode a worldview; readable YAML + descriptions + the scaffold's rationale comments make them contestable/extensible (e.g., non-Western ID regimes). The generator must be prompted to avoid Anglo/Western-default vocabularies — call out in review.
- **Legal & compliance:** Public-policy/identity domain (Aadhaar; `data_protection_law`). `profile_version`/`profile_hash` + git history give a defensible provenance chain for any published figure. Profiles are schema, not PII.
- **Risk & exploitation:** New vectors from Pillar 2 — (a) **prompt-injection via seed sources** into the generator → treat seed text as data; generator only emits schema, which must pass the meta-schema; human reviews. (b) **automation bias** (rubber-stamping) → risk-annotations target it directly. (c) Generated profiles are still **repo files under code review**, never user-supplied at runtime. The fully-autonomous (no-human) path remains out of scope precisely because it removes this gate.
- **Erosion over time:** Failure mode = silent schema drift / un-versioned edits. Addressed by version+hash per run, recompute, and CI validation. Governance scales via `dossier validate`; per-pillar files (granularity A) keep review surface bounded.
- **Economic viability:** Removes per-domain engineering cost (the motivating pain) and lowers authoring cost (scaffold drafts the file). Added cost: a few prompt tokens per extraction (enums/descriptions) + one LLM call per *new domain* scaffold — negligible.
- **Unknown unknowns:** Surface in the adversarial `*.feedback` round — pressure-test (a) prompt-from-schema extraction shifts, (b) meta-schema edge cases, (c) recompute on partial/legacy rows, (d) whether the generator's identity-qualifier rationales are trustworthy enough that humans *can* catch its mistakes.

## Critical files (seams — minimal)
- `src/open_deep_research/factbase/profile.py` — YAML-reading `load()` + meta-schema model; add `description` to `PropertyDef`. *Dataclasses/methods otherwise unchanged.*
- `src/open_deep_research/factbase/registry.py` — YAML-reading `SourceRegistry.load()`.
- `src/open_deep_research/factbase/profiles/*.yaml` — `country_digital_identity.yaml`, `di_source_registry.yaml` (replace `.py`).
- `src/open_deep_research/factbase/scaffold.py` *(new)* — `induce()` shared routine.
- `src/open_deep_research/deep_researcher.py:1284-1319` — compile prompt from profile.
- `src/open_deep_research/factbase/schema.py` (`STEPS`) + `storage.py` — migration + per-run `profile_version`/`profile_hash` stamp.
- `src/open_deep_research/factbase/dossier.py` — `validate` and `scaffold` subcommands.
- `tests/` — interfaces unchanged; **add** `test_factbase_profile_schema.py` (meta-schema accept/reject) and `test_factbase_scaffold.py` (induce → valid annotated draft); CI runs `dossier validate`.

## Verification
1. `uv run pytest` — all existing factbase tests green (interfaces preserved).
2. Meta-schema tests: valid YAML loads; each malformed case raises a clear, located error.
3. `dossier validate`: non-zero on a seeded bad profile, zero on the real ones; wired into `.github/workflows/tests.yml`.
4. Prompt snapshot: rendered extraction prompt contains `scheme_status` values + descriptions.
5. Provenance: extraction into a temp DB stamps `profile_version`/`profile_hash`; recompute(force) updates canonical values after an alias edit.
6. Scaffold: `dossier scaffold` produces a meta-schema-valid, risk-annotated draft from a description (and again with `--seed`).
7. Real run via the `run-research-query` skill (Path B, Tavily wired) on a country brief — facts ingest under the YAML profile, version stamped.

## Suggested implementation order (for the plan stage)
1. Pillar 1 externalization (loader + YAML + meta-schema + `dossier validate`) — keeps interfaces, lowest risk.
2. Prompt compilation + the config flag.
3. Versioning/provenance migration + recompute wiring.
4. Pillar 2 scaffolding (`induce()`, `scaffold` CLI, inline offer).

Pillars 1 and 2 are separable; the plan stage may split them into two implementation plans.

## Out of scope (deferred fast-follows)
- Fully **autonomous** induction (no human gate) + registry reconciliation/promotion of schema elements.
- Profiles as **DB rows** / editing API/UI (files-in-repo first).
- **Composable** multi-profile merging per entity type (granularity B/C).
- Multi-tenant / runtime user-supplied profiles.

## Open questions for the `*.feedback` round
- Meta-schema as Pydantic-in-code vs a checked-in JSON Schema file.
- `profile_version` author-managed vs hash-derived (design proposes both: human `version:` + content `profile_hash`).
- Externalize the registry in this spec or split it (same seam, slightly different review surface).
