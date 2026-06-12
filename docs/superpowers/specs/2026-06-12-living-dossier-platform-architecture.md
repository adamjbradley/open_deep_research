# Living Fact Base — Architecture (v1 feature)

**Date:** 2026-06-12
**Layer:** Architecture (spec-driven development)
**Status:** Draft v3 — round-2 review (4 ADVANCE / 1 code-verified blocker). Source text now reaches
extraction via a **`run_source` side store** (not graph-state notes), one extraction call **per source**.
Pending round-3 convergence check.
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
>
> **Revision note (v3).** Round-2 (code-grounded) confirmed the v2 redesign but found "source-tagged
> notes" reintroduced the round-1 boundary problem (`raw_notes` is `list[str]`, re-joined at the
> supervisor fan-in `deep_researcher.py:488`, so per-source structure can't survive in graph state).
> Fixes: **(a) source text reaches extraction via a `run_source` side store** written at the
> search/researcher-tool layer (run_id, source_url, text, retrieved_at, content_hash) — *not* graph
> state — so no ResearcherState/Supervisor change, and span-verification matches the **original retrieved
> text** (no compression mismatch). **(b) Extraction is one call per source** (flat list of fact records
> across properties), O(sources) not O(sources×properties), with a per-run source ceiling + concurrency
> cap. **(c)** `extract_facts` reads the store by `run_id`, runs on the research path only
> (`final_report_generation → extract_facts → persist_research`; the cache-hit edge skips it); it does
> **not** use `notes` (cleared at `final_report_generation`). **(d) Pure policies receive the tuple's
> current stored facts as input** and emit fully-resolved intents (writer never re-decides). **(e)
> Recompute is forward-only** (re-evaluate status; atomic + per-tuple isolation + resumable on
> `registry_version`; never rewrites closed history). **(f)** dedup keys on **canonical(value,unit)**;
> no-op reruns write no revision.

## 1. Context, existing patterns, invariants
Built inside `deep_researcher`. Reuses: stdlib `aiosqlite` on one DB (`get_db_path`); the
`with_structured_output(PydanticModel)` node pattern (per-backend, CLAUDE.md); the **graph owns the
loop** invariant (`allowed_tools=[]`, models never execute tools); subscription backends. `research_runs`
already stores the per-run report = the vision's immutable run report (P2), kept. The LLM prose-merge
(`_merge_dossier`/`current_report`/`dossier_versions`) is **retired** for fact subjects.

## 2. Where it hooks into the graph (corrected)
The supervisor fan-out is unchanged. A **new `extract_facts` node runs once** on the research path,
inserted between the existing edges:

```
… supervisor_subgraph → final_report_generation → extract_facts → persist_research → END
        (cache-hit path:  answer_from_dossier → persist_research   — skips extract_facts)
```

**Source text via a side store (not graph state).** The round-1/round-2 boundary problem is that
`raw_notes` is `list[str]` and is *re-joined* at the supervisor fan-in (`deep_researcher.py:488`), so
per-source structure cannot survive in LangGraph state. Instead, the **search/researcher-tool layer
writes each retrieved source to a `run_source(run_id, source_url, text, retrieved_at, content_hash)`
table** as it fetches them — persistence is the carry channel. `extract_facts` then reads
`run_source` **by `run_id`** (it does *not* use `notes`, which `final_report_generation` clears, or the
flattened `raw_notes`). This needs **no ResearcherState/SupervisorState change** and gives extraction
the **original retrieved text**, so `evidence_span` is verified as an exact substring of the true
source (§4) — and the evidence record's `retrieved_at`/`doc_identity` come straight from `run_source`.

`extract_facts` runs the `FactExtractor` **once per source** (§4), collects candidate facts, and hands
them to the **ingestion service** (§5) which `persist_research` calls. **Single writer:** all fact
writes happen in this one end-of-run node (never concurrently per researcher), so SQLite contention is
moot; `busy_timeout` + bounded retry are set defensively.

## 3. Component model (ports & adapters)

| Port | Responsibility | v1 adapter | Notes |
|---|---|---|---|
| `RunSourceStore` | persist/read per-source retrieved text per run | aiosqlite (`run_source`) | written at tool layer; read by `extract_facts` |
| `FactExtractor` | one source's text → candidate facts (**all profile properties, flat records**) | LLM structured-output | one call per source (§4) |
| `EntityResolver` | `instance_name` → canonical key, or **unresolved** | ISO-3166 + alias manifest | miss ⇒ quarantine, never auto-create |
| `FactIdentity` | `tuple_key` hash + `canonicalize(value,unit)` + value-equality | pure module | **single owner** of identity/equality |
| `SourceRegistry` | source → (type,property) tier + flags + **registry_version** | profile data-file | versioned |
| `FactStore` | split: `FactWriter` (atomic ingest tx) + `FactQuery` (read) | aiosqlite | narrow interfaces |
| `ConflictPolicy` / `PromotionPolicy` | **pure decision functions** → return intents | pure module | receive the tuple's stored facts as input |

Pure policies **receive the current tuple's stored facts as input** and **return fully-resolved intents**
(e.g. `Promote(fact_id)`, `OpenConflict([...])`, `Supersede(old_id)`, `AutoClose(conflict_id)`); the
`FactWriter` only *persists* them inside the transaction — it never re-reads state or re-decides ordering
(fixes the round-1 leak and the round-2 "writer becomes a second decision site" risk). The **ingestion
application service** orchestrates resolve → identity → validate → registry → load-tuple → policies →
writer; `persist_research` just calls it (thin).

## 4. Fact extraction contract
**One call per source**, returning a **flat list of fact records** (each tagged with its `property` from
the closed profile set). The records are flat (no nested qualifier objects), so Gemini/Codex envelope
coercion stays robust; the task is **classify against closed enums**, not generate. This is O(sources)
calls per run (not O(sources×properties)); a **per-run source ceiling** + a **concurrency cap** bound
cost/latency.
```
FactRecord {                 # one extracted fact from one source
  property: enum             # ∈ profile property set (closed)
  instance_name: str
  value: str                 # raw value as stated; typing/validation downstream
  unit: str | null
  as_of: str | null          # year in v1
  q_population_basis: enum|null   # flat, nullable identity-qualifier fields (per property; null ⇒ abstain)
  q_coverage_kind:    enum|null
  q_basis: enum|null   q_stage: enum|null   q_scope: enum|null   q_jurisdiction: str|null
  evidence_span: str         # must be a verbatim substring of the source's run_source.text
}
ExtractionResult { run_id: int, source_url: str, facts: list[FactRecord] }
```
**Abstain:** a `q_*` field is emitted only when the source explicitly states it (or a direct synonym);
else `null` ⇒ `unspecified` for that qualifier (⇒ its own non-promotable tuple, §5). **Post-coercion
validation** (every backend): drop any `FactRecord` that (a) fails schema/enum, (b) has `evidence_span`
**not found (whitespace-normalized) verbatim** in that source's `run_source.text`, or (c) fails
`property_def.validation` (range/regex). **Drop-rate guardrail:** if a property's drop ratio exceeds a
threshold, log a warning (catches over-tight validation / coercion collapse). A truncated/partial JSON
response for a source is retried once then skipped-with-log; **other sources are unaffected**
(per-source isolation).

## 5. Ingestion protocol (transactional + idempotent)
For each candidate fact, the application service: `EntityResolver.resolve` (miss → quarantine table,
not a phantom entity); `FactIdentity.tuple_key` (instance_id + property + sorted non-null `q_*`; any
`unspecified` ⇒ its own non-promotable tuple); `SourceRegistry.tier` (+ record `registry_version`);
then `ConflictPolicy`/`PromotionPolicy` produce intents. **`FactWriter.ingest_run(run_id, intents)`
runs in ONE transaction:** insert facts + evidence + `fact_revision` + conflict changes atomically;
either all land or none (no provenance-less facts, no half-open conflicts). **Per-fact isolation:** a
single bad fact is dropped+logged and never aborts the batch. **Idempotency / re-run dedup:** a fact is
deduped on `(tuple_key, as_of, canonical(value,unit), source_id)` (canonical, not raw value, so unit
variants don't duplicate); re-running the same source/value is a **no-op that writes no `fact_revision`**
— only a genuine value change for that source writes a revision.
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
- **Recompute:** bumping `registry_version` (or a tier edit) triggers a **forward-only re-evaluate
  pass** over affected tuples (re-run pure policies over stored facts; update admission/conflict status
  only) — no re-research, no LLM calls. It runs under the **same atomic-tx + per-tuple isolation**
  contract as `ingest_run`, is **resumable** on `registry_version` (bounded batches, never half-applied),
  and **never rewrites closed historical conflicts/resolutions** — it appends new revisions only for
  genuine status changes.

## 7. Data model (SQLite + migrations)
A **versioned migration framework** replaces the ad-hoc `executescript(_SCHEMA)` (storage.py:94): a
`schema_migrations(version)` table + ordered migration steps, so new tables land safely on a populated
DB. Tables (indicative): `run_source(run_id, source_url, text, retrieved_at, content_hash)` (the
extraction input + evidence source, written at the tool layer), `entity_type`,
`entity_instance(canonical_key, aliases_json)`,
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
- **Cost:** extraction is **one structured-output call per source** at end-of-run (O(sources), not
  sources×properties), under a **per-run source ceiling** + concurrency cap; no per-token API cost.
  Pure engines negligible. Recompute is LLM-free.
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
5. `run_source` store written at the search/researcher-tool layer; `FactExtractor` one-call-per-source
   + post-coercion validation + span verification against `run_source.text`.
6. `extract_facts` node (reads `run_source` by run_id) + ingestion application service + `FactWriter`
   atomic tx; wire into the graph on the research path (not the cache-hit edge).
7. `FactQuery` + `dossier` CLI (show → compare → export), one render path.
8. Instrumentation (metrics, false-conflict + drop-rate audit); recompute pass.

## 12. Open questions → implementation
- `tuple_key` hash spec + qualifier canonicalization (sorted, case-folded enums); `canonicalize(value,unit)`.
- `run_source` write point in the tool layer (which search adapters; dedup of the same URL across
  researchers); span-verification whitespace-normalization rule.
- ISO-3166 alias manifest source + how `unresolved_instance` is reviewed.
- Extraction prompt calibration for "explicitly states" (the false-conflict / drop-rate dial).
- Per-property trust thresholds; registry recompute trigger granularity.
- Source acquisition for ID4D/GSMA (API vs scrape vs manual seed).

---

*Next step: round-3 convergence check on this Architecture, then the implementation-plan layer
(`writing-plans`), built TDD per §11.*
