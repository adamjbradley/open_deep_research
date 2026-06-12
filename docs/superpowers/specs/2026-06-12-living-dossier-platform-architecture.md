# Living Fact Base — Architecture (v1 feature)

**Date:** 2026-06-12
**Layer:** Architecture (spec-driven development)
**Status:** Draft v2 — incorporated round-1 review (5 reviewers, 4 ANOTHER ROUND). Pending round-2.
**Builds on:** Vision+Principles v8, Feature Spec v4.
**Scope:** Digital Identity pillar × ~15–20 country cohort; per-country dossier + cross-country compare
(read-only) + CSV/MD export.

> **Revision note (v2).** Round-1 (code-grounded) review found the v1 ingestion path unbuildable and
> under-specified. Fixes: **(1)** extraction is a **single post-supervisor node, per-source** over
> retained source-tagged notes (the per-researcher path didn't survive `ResearcherOutputState` /
> `supervisor_tools`); **(2) evidence spans are substring-verified** against the retained source text,
> else rejected; **(3)** a **transactional, idempotent ingestion protocol** (one atomic write per run,
> per-fact isolation, single writer); **(4)** a first-class **`EntityResolver`** port (no phantom
> entities); **(5)** **flat per-property** extraction schema (not a nested qualifier object-list) for
> robust Gemini/Codex coercion + per-fact post-coercion validation; **(6)** a **`FactIdentity`** service
> and an **ingestion application service** (persist_research is no longer a god-aggregator); a real
> **migration framework**; **(7)** `dossier` is a **net-new CLI program**; registry **versioning +
> recompute**; **soft-delete** columns; retention notes.

## 1. Context, existing patterns, invariants
Built inside `deep_researcher`. Reuses: stdlib `aiosqlite` on one DB (`get_db_path`); the
`with_structured_output(PydanticModel)` node pattern (per-backend, CLAUDE.md); the **graph owns the
loop** invariant (`allowed_tools=[]`, models never execute tools); subscription backends. `research_runs`
already stores the per-run report = the vision's immutable run report (P2), kept. The LLM prose-merge
(`_merge_dossier`/`current_report`/`dossier_versions`) is **retired** for fact subjects.

## 2. Where it hooks into the graph (corrected)
The supervisor fan-out is unchanged. After the supervisor completes and `notes`/`raw_notes` are
assembled (top-level `AgentState`), a **new `extract_facts` node runs once**, before/within
`persist_research`:

```
… supervisor_subgraph → final_report_generation → extract_facts → persist_research → END
```

`extract_facts` reads the run's **source-tagged notes** (see §3), runs the `FactExtractor` **per
source**, and emits an `ExtractionResult` into `AgentState` (a new field with a list-append reducer).
`persist_research` then calls the **ingestion service** (§5). This needs **no change to
ResearcherState/SupervisorState** — the round-1 blocker is avoided. **Single writer:** all fact writes
happen in this one node at end-of-run (not concurrently per researcher), so SQLite contention is moot;
we still set `busy_timeout` + a bounded retry defensively.

**Source-tagged notes (build item):** today `raw_notes` is a `str`-join (`compress_research`,
deep_researcher.py:716). To extract per-source with verifiable spans, the researcher must retain
**`{source_url, text}` note segments** (a small additive change to compression — keep the per-source
tool-result text, not only the joined prose). `FactExtractor` runs against each segment's `text`; the
`evidence_span` is then **verified as an exact substring of that segment** (§4) — impossible to
hallucinate undetected.

## 3. Component model (ports & adapters)

| Port | Responsibility | v1 adapter | Notes |
|---|---|---|---|
| `FactExtractor` | one source-note → candidate facts for **one property at a time** | LLM structured-output | flat schema (§4) |
| `EntityResolver` | `instance_name` → canonical key, or **unresolved** | ISO-3166 + alias manifest | miss ⇒ quarantine, never auto-create |
| `FactIdentity` | `tuple_key` hash + `canonicalize(value,unit)` + value-equality | pure module | **single owner** of identity/equality |
| `SourceRegistry` | source → (type,property) tier + flags + **registry_version** | profile data-file | versioned |
| `FactStore` | split: `FactWriter` (atomic ingest tx) + `FactQuery` (read) | aiosqlite | narrow interfaces |
| `ConflictPolicy` / `PromotionPolicy` | **pure decision functions** → return intents | pure module | caller (writer) applies writes |

Pure policies **return decisions** (e.g. `Promote(fact_id)`, `OpenConflict([...])`,
`Supersede(old_id)`); the `FactWriter` applies them inside the transaction — so "pure" policies never
touch store columns (fixes round-1 decoupling leak). The **ingestion application service** orchestrates
resolve → identity → validate → registry → policies → writer; `persist_research` just calls it (thin).

## 4. Fact extraction contract
**Per property, flat schema** (avoids nested qualifier object-lists that break Gemini/Codex coercion):
```
ExtractedFact {            # one source, one property
  instance_name: str
  value: str               # raw value as stated; typing/validation downstream
  unit: str | null
  as_of: str | null        # year in v1
  q_<qualifier>: enum|null  # FLAT, property-specific fields, e.g. q_population_basis, q_coverage_kind
  evidence_span: str        # must be substring of the source note
}
ExtractionResult { property: str, source_url: str, facts: list[ExtractedFact] }
```
The node loops `(source-note × property-in-profile)`; each call returns facts for **one** property,
keeping the schema flat and the task a **classify** (closed enums), not generate. **Abstain:** a
`q_*` enum is emitted only when the source explicitly states it (or a direct synonym); else `null`
(⇒ `unspecified` tuple, §5). **Post-coercion validation** (every backend, esp. Gemini/Codex):
drop any `ExtractedFact` that (a) fails schema/enum, (b) has `evidence_span` **not found verbatim** in
the source note, or (c) fails `property_def.validation` (range/regex). **Drop-rate guardrail:** if a
property's drop ratio exceeds a threshold, log a warning (catches over-tight validation / coercion
collapse). Truncated/partial JSON from a backend → that one `(source,property)` call is retried once
then skipped-with-log; **other facts are unaffected** (per-call isolation).

## 5. Ingestion protocol (transactional + idempotent)
For each candidate fact, the application service: `EntityResolver.resolve` (miss → quarantine table,
not a phantom entity); `FactIdentity.tuple_key` (instance_id + property + sorted non-null `q_*`; any
`unspecified` ⇒ its own non-promotable tuple); `SourceRegistry.tier` (+ record `registry_version`);
then `ConflictPolicy`/`PromotionPolicy` produce intents. **`FactWriter.ingest_run(run_id, intents)`
runs in ONE transaction:** insert facts + evidence + `fact_revision` + conflict changes atomically;
either all land or none (no provenance-less facts, no half-open conflicts). **Per-fact isolation:** a
single bad fact is dropped+logged and never aborts the batch. **Idempotency / re-run dedup:** a fact is
deduped on `(tuple_key, as_of, value, source_id)`; re-running the same source/value does not create a
duplicate or a phantom revision (only a genuine value change for that source writes a revision).
`persist_research` wraps the call so a failure **logs but never crashes the completed run** (matches
today's best-effort contract).

## 6. Conflict, promotion, lifecycle (pure decisions)
- **Conflict:** within a `tuple_key`, partition by `as_of` year; among **trust-bar-meeting** facts in a
  bucket, ≥2 distinct values (`FactIdentity` equality) ⇒ `OpenConflict`. Lower-tier disagreement is
  stored/surfaced, never promotion-blocking. **Auto-close:** a conflict closes when its members
  collapse to one value (correction/supersession) — so phantom conflicts from a since-fixed extraction
  don't block forever.
- **Promotion:** `trusted` iff source tier ≥ property threshold AND no `unspecified` required qualifier
  AND no open conflict in its (tuple_key, as_of) bucket.
- **Version vs supersede:** newer `as_of` ⇒ prior current version `superseded` (kept in history). A
  **dated** value always orders above `as_of: unknown` (deterministic null rule); two facts at equal
  `as_of` differing in value are a conflict, not a supersede.
- **Recompute:** bumping `registry_version` (or a tier edit) triggers a **re-evaluate pass** over
  affected tuples (re-run pure policies over stored facts) — no re-research, no LLM calls.

## 7. Data model (SQLite + migrations)
A **versioned migration framework** replaces the ad-hoc `executescript(_SCHEMA)` (storage.py:94): a
`schema_migrations(version)` table + ordered migration steps, so new tables land safely on a populated
DB. Tables (indicative): `entity_type`, `entity_instance(canonical_key, aliases_json)`,
`unresolved_instance` (quarantine), `property_def(value_kind, identity_qualifiers, validation)`,
`source(registry_version, tier, flags, soft_deleted_at)`, `fact(tuple_key, as_of, value, unit,
admission, lifecycle, confidence, source_id, run_id, soft_deleted_at)`, `evidence(fact_id, quoted_span,
source_url, doc_identity, retrieved_at)`, `fact_revision(fact_id, change, cause, why, created_at)`,
`conflict(tuple_key, as_of, status)`, `conflict_member`. **Soft-delete** columns make redaction/
tombstoning (vision §8) real, not aspirational. **Retention:** evidence spans are bounded length and
de-duplicated by `(source_url, hash(span))` to cap bloat; a prune policy is configurable.

## 8. Read-only surfaces (net-new CLI program)
`dossier` is a **new CLI entry point** (console-script + a callable usable from the dev server) — the
repo has none today; it opens the DB read-only via `FactQuery` and renders through **one canonical
render path** (the rendering contract, vision §5): never present provisional/contested as established.
- `dossier show <country>` — fact table: per property, one row per (qualifier-tuple, current version):
  value (`~prov` if provisional, `⚠`+both if conflicted), source, as_of, evidence handle.
- `dossier compare <property>` — cohort rows; one column **per qualifier tuple**; footer states
  coverage (N value / N unknown / column bases).
- `--format csv|md` — every row carries value+source+as_of+qualifiers; provisional/conflicted labelled.

## 9. Required-coverage
- **Decoupling:** §3 ports; `FactIdentity` is the single identity owner; policies are pure and return
  intents; ingestion service orchestrates; `FactStore` split into writer/query. Swap test passes for
  store, extractor backend, and equality rule (now all behind ports).
- **Resilience/degradation:** per-call extraction isolation + retry-once; per-fact drop+log; one
  atomic ingest tx; single writer + `busy_timeout`; `persist_research` never crashes the run; degraded
  synthesis (P7) for thin trusted data; drop-rate guardrail.
- **Observability/anti-metrics:** coverage, groundedness, **audited false-conflict rate** + a new
  **false-rejection (drop-rate)** signal, trusted/provisional ratio. Never optimize raw fact count.
- **Explainability:** every intent writes a `fact_revision(why)`; every fact has an `evidence` row with
  a verified span.
- **Security/privacy:** local single-user DB; **new egress acknowledged** — `extract_facts` sends
  source text to the model backend (existing providers, but new volume; governed by provider retention).
  Soft-delete + redaction hooks for erasure.
- **Cost:** extraction is `sources × properties` structured-output calls at end-of-run; bounded by
  cohort×6 and de-duplicated per source; no per-token API cost. Pure engines negligible. Recompute is
  LLM-free.
- **Vendor:** extractor behind `FactExtractor` (any backend); source acquisition behind `SourceRegistry`.

## 10. Migration
Legacy `current_report`/`dossier_versions` prose **left as-is** (readable); fact base starts empty;
**no back-fill** (would fabricate provenance, vision P3/§9). Schema migrations are additive.

## 11. Build sequence (TDD)
1. Migration framework + schema; `FactIdentity` + `ConflictPolicy` + `PromotionPolicy` as pure modules
   with unit tests first.
2. `property_def` + Digital-Identity profile (property set + qualifiers + validation).
3. `EntityResolver` (ISO-3166 + alias manifest) + quarantine.
4. `SourceRegistry` data file + adapter (+ versioning).
5. Source-tagged notes change in compression; `FactExtractor` per-(source,property) + post-coercion
   validation + span verification.
6. `extract_facts` node + ingestion application service + `FactWriter` atomic tx; wire into the graph.
7. `FactQuery` + `dossier` CLI (show → compare → export), one render path.
8. Instrumentation (metrics, false-conflict + drop-rate audit); recompute pass.

## 12. Open questions → implementation
- `tuple_key` hash spec + qualifier canonicalization (sorted, case-folded enums).
- Exact source-tagged-note representation from compression; span-verification tolerance (whitespace).
- ISO-3166 alias manifest source + how `unresolved_instance` is reviewed.
- Extraction prompt calibration for "explicitly states" (the false-conflict / drop-rate dial).
- Per-property trust thresholds; registry recompute trigger granularity.
- Source acquisition for ID4D/GSMA (API vs scrape vs manual seed).

---

*Next step: round-2 review of this Architecture, then the implementation-plan layer (`writing-plans`),
built TDD per §11.*
