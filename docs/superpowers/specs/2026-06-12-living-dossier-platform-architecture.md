# Living Fact Base — Architecture (v1)

**Date:** 2026-06-12
**Layer:** Architecture (spec-driven development)
**Status:** Draft v1 — pending multi-agent review
**Builds on:** Vision+Principles v8 (`...-design.md`) and Feature Spec v4 (`...-features.md`)
**Scope:** v1 = Digital Identity pillar × ~15–20 country cohort; per-country dossier + cross-country
compare (read-only) + CSV/MD export.

---

## 1. Context, existing patterns, invariants

The fact base is built **inside the existing `deep_researcher` LangGraph app**, reusing its patterns:
- **Storage:** `storage.py` uses stdlib `aiosqlite` against one SQLite file (`get_db_path`). New tables
  are added to the **same DB** (decision: *new tables alongside*). `research_runs` already stores the
  per-run report — that **is** the vision's immutable run report (P2); we keep it. The LLM prose-merge
  (`_merge_dossier` → `subjects.current_report`, `dossier_versions`) is **retired** for v1 fact subjects
  (left in place only for any non-fact legacy use); the dossier view renders from facts.
- **Structured output:** every node calls `model.with_structured_output(PydanticModel)` (e.g.
  `KnowledgeAssessment`). Per CLAUDE.md this is reimplemented per backend (Claude native; Gemini/Codex
  coerce a JSON envelope). Fact extraction is **one more structured-output call** — no new mechanism.
- **Invariant — the graph owns the loop:** extraction *selects/produces structured data*; it never
  executes tools. Models are invoked with `allowed_tools=[]`. Preserved.
- **Backends bill against a subscription:** no new API-key paths.

## 2. Component model (ports & adapters — decoupling)

Business logic must not depend on infrastructure. Five **ports** (interfaces), each with a v1 adapter:

| Port | Responsibility | v1 adapter |
|---|---|---|
| `FactExtractor` | compressed notes → candidate facts (+spans) | LLM structured-output over the per-researcher backend |
| `FactStore` | persist/query facts, tuples, conflicts, history, evidence | `aiosqlite` adapter (same DB) |
| `SourceRegistry` | source → (type,property) trust tier + flags | data-file adapter (domain profile) |
| `ConflictPolicy` | value-equality + tuple grouping + conflict open/close | pure-Python adapter (exact-match v1) |
| `PromotionPolicy` | provisional→trusted / demote decisions | pure-Python adapter (P7 rule) |

Surfaces (`dossier show`, `dossier compare`, export) and the engines depend on the **ports**, not the
SQLite/LLM specifics — so the store, extractor model, or equality rule can be swapped without touching
business logic (the swap test). The conflict/promotion policies are **pure functions** (no I/O) →
unit-testable in isolation.

## 3. Data model (SQLite)

New tables (names indicative). Existing `subjects`/`research_runs` kept; `research_runs.id` is the run
reference.

```
entity_type(id, name, profile_json)                    -- 'country'; profile = property defs + qualifiers
entity_instance(id, type_id, canonical_key, name,      -- canonical_key = ISO-3166 alpha-3 for countries
                aliases_json)                           -- alias resolution (Türkiye/Turkey…)
property_def(id, type_id, name, value_kind,            -- 'id_coverage_pct', 'percentage', …
             identity_qualifiers_json,                  -- ['population_basis','coverage_kind','measured_modeled']
             validation_json)                           -- ranges/enums/regex (sanity check, §5 FS)

fact(id, instance_id, property_id,
     qualifiers_json,         -- the non-temporal identity qualifiers (the TUPLE discriminator); 'unspecified' allowed
     tuple_key,               -- deterministic hash of (instance, property, sorted qualifiers) — the conflict-group key
     as_of,                   -- version dimension (year in v1); nullable => 'unknown'
     value, unit,
     source_id,
     admission,               -- 'provisional' | 'trusted'
     lifecycle,               -- 'current' | 'stale' | 'superseded'
     confidence,              -- computed, heuristic
     run_id,                  -- REFERENCES research_runs(id)
     created_at)
evidence(id, fact_id, quoted_span, source_url, doc_identity, retrieved_at)
fact_revision(id, fact_id, change, cause, why, created_at)   -- append-only (vision P6)
conflict(id, tuple_key, as_of, status, opened_run_id, resolved_run_id, created_at)  -- 'open'|'resolved'
conflict_member(conflict_id, fact_id)
source(id, url/domain, registry_tier_json, flags_json)       -- modeled / incentivized flags
```

**Identity:** the conflict group is `tuple_key` = hash(instance, property, sorted non-temporal
qualifiers). `as_of` is **not** in `tuple_key` (it is the version axis). A fact with any required
qualifier = `unspecified` gets a *distinct* `tuple_key` (so it never compares against specified-basis
facts) and is flagged non-promotable.

## 4. Fact extraction (the load-bearing deliverable)

**Placement:** a `FactExtractor` call **per researcher**, over that researcher's compressed,
citation-bearing notes (where source spans are tightest), emitted into `ResearcherState`; aggregated
in `persist_research`. (Decision: per-researcher over compressed notes.)

**Output schema** (Pydantic, structured output):
```
ExtractedFact { instance_name, property_name, value, unit?,
                qualifiers: {name: value|"unspecified"},
                as_of?, source_url, evidence_span }   # value bound to an exact quoted span
ExtractionResult { facts: list[ExtractedFact] }
```
**Abstain calibration (the contract):** the prompt instructs the model to emit a qualifier **only when
the source explicitly states it (or a direct synonym); when in doubt, `unspecified`** — never inferred.
A value is emitted only with a supporting `evidence_span` (AC1.4); no span → no fact. Property/qualifier
names are constrained to the `property_def` vocabulary (closed enums) to keep extraction a *classify*,
not *generate*, task — which also makes the Gemini/Codex JSON-envelope coercion robust.

**Aggregation (`persist_research`):** map `instance_name`→`entity_instance` (alias resolution),
validate values against `property_def.validation_json` (drop implausible), compute `tuple_key`, look up
`source` tier via `SourceRegistry`, then hand tuples to the Conflict + Promotion engines.

## 5. Conflict & promotion engines (pure functions)

**ConflictPolicy.group_and_detect(facts):** group by `tuple_key`; within a group, partition by
`as_of` year; for each (tuple_key, as_of) bucket, among **trust-bar-meeting** facts, if ≥2 distinct
values under **exact-match** → open a `conflict` linking those facts. Lower-tier facts are stored and
surfaced but never open a promotion-blocking conflict. *(v1 equality = strict string-equal within
identical unit; normalization/tolerance deferred — a single `canonicalize(value,unit)` seam exists,
identity function in v1.)*

**PromotionPolicy.evaluate(fact, group):** promote to `trusted` iff (source tier ≥ property threshold)
AND (no `unspecified` required qualifier) AND (no open conflict in its (tuple_key, as_of) bucket). A
newer `as_of` supersedes the prior current version (→ `lifecycle=superseded`, kept in history). A
later trust-bar fact disagreeing at the same as_of demotes + opens a conflict. Every transition writes
a `fact_revision` (what/cause/why) — this is also the **explainability** record (§8).

## 6. Source registry (data, not code)

A versioned file in the domain profile (`profiles/country-digital-identity.yaml`): maps source
domain/dataset → per-(type,property) tier (`authoritative`/`reputable`/`unvetted`) + flags
(`modeled`, `incentivized`). Encodes domain corrections (ID4D=modeled; national-operator coverage not
ranked above academic). `SourceRegistry` resolves a fact's `source_url` → tier; unknown domains →
`unvetted`. **No real-time URL classification.** Editable without code changes (inclusion/bias seam,
vision §8).

## 7. Read-only surfaces

CLI commands (and the equivalent callable for the dev server), rendering through **one canonical
render path** (the rendering contract, vision §5):
- `dossier show <country>` — fact table: per property, one row per (qualifier-tuple, current version):
  value, `~prov` if provisional, `⚠` + both values if conflicted, source, as_of, evidence handle.
- `dossier compare <property>` — cohort rows; one column **per qualifier tuple** (e.g. per
  `population_basis`), never merging mismatched bases; footer states coverage (N value / N unknown).
- `--format csv|md` — every row carries value+source+as_of+qualifiers; provisional/conflicted labelled.
The renderer is the single place that enforces "never present provisional/contested as established."

## 8. Required-coverage

- **Decoupling:** §2 ports; conflict/promotion are pure functions; swap test passes.
- **Graceful degradation:** extraction failure on a researcher → that run yields fewer facts, logged,
  never crashes the run (matches today's best-effort `persist_research`). Backend structured-output
  failure → retry then skip-with-log. Degraded synthesis (vision P7) handles thin trusted data.
- **Observability / anti-metrics:** instrument coverage, groundedness, **audited false-conflict rate**
  (the key health signal), trusted/provisional ratio. **Never optimize raw fact count** (anti-metric).
- **Explainability:** every promotion/demotion/conflict writes a `fact_revision` with a `why`; every
  fact has an `evidence` row. A user can always answer "why is this the value, and who says so."
- **Security / privacy:** single-user local SQLite; no new network egress beyond existing search. PII
  is low for country-level DPI facts, but the evidence store keeps source excerpts → redaction/
  tombstoning hook on `fact`/`evidence` (vision §8) deferred but schema-ready (soft-delete columns).
- **Cost model:** +1 structured-output call per researcher (bounded by `max_concurrent_research_units`)
  + pure-Python engines (negligible). Cohort×6 properties is bounded; no per-token API cost
  (subscription backends). Conflict detection is O(facts-in-tuple), not O(N²-global).
- **Vendor dependency:** extractor is behind `FactExtractor`; works on any of the three backends via
  the existing per-role selection. Source data behind `SourceRegistry` (swap ID4D/GSMA acquisition).

## 9. Migration
Legacy `subjects.current_report` / `dossier_versions` prose is **left as-is** (readable history); the
fact base starts empty and populates on new runs. **No back-fill** — extracting facts from old prose
would fabricate provenance/evidence-spans (forbidden, vision P3/§9). A future migration could re-run
extraction over stored run reports, but only by attaching real source spans.

## 10. Build sequence (for the implementation plan)
1. Schema + `FactStore` (migrations; pure-fn engines with unit tests first — TDD).
2. `property_def` + domain profile for Digital Identity (the §2.1 property set + validation).
3. `SourceRegistry` data file + adapter.
4. `FactExtractor` node in the researcher subgraph + aggregation in `persist_research`.
5. Conflict + promotion wiring; `fact_revision` explainability.
6. `dossier show` → `dossier compare` → export (one render path).
7. Instrumentation (metrics + false-conflict audit harness).

## 11. Open questions → implementation
- Exact `tuple_key` hashing + qualifier canonicalization (sorted, case-folded enums).
- `value_kind`-specific `canonicalize()` rules (when post-v1 normalization lands).
- ISO-3166 alias map source; how `entity_instance.aliases_json` is seeded.
- Extraction prompt calibration for "explicitly states" (the false-conflict-rate dial).
- Source acquisition for ID4D/GSMA (API vs scrape vs manual seed) — connector design.
- Whether `dossier show` triggers refresh (flywheel) — default: explicit run only (cost-bounded).

---

*Next step: multi-agent review of this Architecture, then the implementation-plan layer
(`writing-plans`), built TDD per the build sequence.*
