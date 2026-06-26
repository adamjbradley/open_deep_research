# Design: required-qualifier resolution

**Status:** designed (brainstormed). First of two sub-projects from the gap-prioritization
follow-up thread; the second (gap-loop hard cap for very large profiles) is deferred.

## Problem

A profile property can declare `required_qualifiers` (e.g. `data_protection_law` requires
`stage` ∈ {enacted, in_force}; `id_coverage_pct` requires `population_basis`). A fact resolves
only when it has a value **and** every required qualifier. Today a fact can land its value but
miss a required qualifier, leaving the property stuck as `missing_qualifier` indefinitely.

Confirmed live on 2026-06-26: the Estonia dossier landed `data_protection_law = true` but with
`{stage: null}` — the source classified data protection qualitatively rather than citing a
statute with an enactment/in-force date, so the model emitted the value but no `stage`.

Two contributing causes:
1. **The extraction prompt never marks which qualifiers are required**, and instructs "emit a
   qualifier ONLY if the source explicitly states it (do not guess)" — so an implied-but-not-
   stated required qualifier is dropped.
2. **Research doesn't target the missing axis** — the gap round re-researches the property
   broadly rather than asking specifically for the in-force date from a primary source.

## Goal

A fact lands its value **and** its required qualifiers, via three levers in priority order:
**capture explicit → targeted research → bounded inference (marked, lower-trust)**. The
property then fully resolves instead of churning as `missing_qualifier`.

## Decisions (from brainstorming)

- **Bounded inference is allowed** for *required* qualifiers from clear contextual cues (a law
  that "applies"/"governs" → `stage=in_force`). Optional qualifiers keep the strict
  "explicit-only" rule.
- **Inferred qualifiers are marked and lower-trust** (not treated identically to stated, and
  not held provisional-until-corroborated). Provenance stays honest.
- **Research is surgical:** when a required qualifier is missing, the gap round emits a
  *targeted sub-query* for that specific axis, preferring primary sources.
- **A post-extraction resolver** owns the inference, keeping bulk extraction lean.

## Architecture

A new node sits between extraction and the completeness/sufficiency check, mode-agnostic
(required qualifiers matter in facts-first mode too):

```
… → extract_facts → resolve_required_qualifiers (NEW) → route_after_extract
                                                          → assess_completeness | assess_sufficiency | persist_research
```

The resolver inserts on the existing `extract_facts → route_after_extract` edge, so it runs in
both whole-profile and facts-first modes and a no-op (no missing required qualifiers) costs
nothing.

### End-to-end flow

1. `extract_facts` does bulk per-source extraction as today, capturing explicitly-stated
   qualifiers and ingesting facts.
2. **`resolve_required_qualifiers`** finds facts that still lack a required qualifier and, for
   each missing axis, resolves it from the fact's evidence span: **stated** (in the source),
   **inferred** (strongly implied → marked, lower confidence), or **null** (no basis).
3. `assess_completeness` / `assess_sufficiency` re-evaluate. A required qualifier now present
   (stated or inferred) → **resolved**, so the loop can terminate; inferred-ness and lower
   confidence are retained for provenance + conflict-detection.
4. If the resolver returned `null`, the property stays `missing_qualifier` → the gap round
   emits a **targeted sub-query** for that axis → a later round fetches a primary source and
   step 1 captures the qualifier **explicitly**.

Inference is the fast-path that stops churn; targeted research is the fallback for the
can't-even-infer case. A successful inference resolves the property — no dedicated round is
spent upgrading an inferred value to explicit (consistent with "mark inferred", not
"provisional until corroborated"). If a later round researching a *different* gap happens to
surface an explicit value, conflict-detection upgrades it for free.

## Components

### 1. Catalog: mark required qualifiers

`compile_property_catalog` surfaces required-ness so both the bulk extractor and the resolver
know which axes are mandatory, e.g.:

```
- data_protection_law (boolean): … | qualifiers: stage=[enacted, in_force] (REQUIRED); scope=[comprehensive, sectoral]
```

### 2. `resolve_required_qualifiers` node

- **Selects** the run's facts with a value but ≥1 absent required qualifier (the same condition
  the completeness ledger uses for `missing_qualifier`).
- **For each fact + missing axis**, one focused model call via an **injected `model_call`**
  (testable without a live model; routable to a stronger model than bulk extraction — the task
  is tiny and accuracy-sensitive):
  > Property `<name>` (value `<value>`) for `<instance>`. Evidence: "`<evidence_span>`". The
  > required qualifier `<qualifier>` must be one of `<enum>`. If the evidence **explicitly
  > states** it → `{value, basis: "stated"}`; if it **strongly implies** it →
  > `{value, basis: "inferred"}`; if neither → `{value: null}`.
- **Scope (v1):** resolve from the fact's own evidence span. Cross-source qualifier evidence
  (value from source A, stage from source B) is a deliberate future enhancement.
- **Best-effort:** a model error or `null` leaves the fact unchanged; never fails the run.
- **Bounded + logged:** a per-run cap limits resolver calls; if it trips, it is logged (no
  silent truncation).
- **Idempotent:** a fact with no missing required qualifier is skipped, so re-running each gap
  round is safe.

### 3. Storage: inferred provenance + lower trust

The resolver runs post-ingest, so it **updates** the existing fact row and writes a
`fact_revision` row (audit trail):

- `qualifiers_json` gains the resolved value.
- A new nullable `qualifier_provenance_json` column on the `fact` table (added via a schema
  migration) records inferred axes, e.g. `{"stage": "inferred"}` (stated qualifiers are the
  default and unmarked).
- An inferred required qualifier **downgrades the fact's `confidence`** (existing column) to a
  defined "inferred" tier below stated, and keeps `admission = provisional`, so
  conflict-detection/rendering prefer a later explicit, higher-confidence value.

### 4. Research: targeted qualifier sub-query

When `assess_completeness` finds a property still `missing_qualifier` (resolver returned
`null`), it emits an axis-specific directive into the gap brief instead of the bare
`<name> (missing_qualifier)`:

> `data_protection_law`: the value is known, but its required `stage` (enacted vs in_force) is
> unconfirmed — find a **primary/official** source (the statute, act, or regulator) stating the
> enactment or in-force date.

### 5. Completeness integration

Because the resolver runs *before* the completeness check, the ledger simply sees the filled
qualifier and returns `resolved`. An inferred qualifier satisfies the required-qualifier check
for **loop termination**; its lower confidence lives in rendering/conflict-detection, not loop
control. The only completeness change is the targeted gap-directive wording (component 4).

## Error handling

- Resolver errors/timeouts/`null` → fact unchanged, non-fatal (mirrors `extract_facts`).
- No fabrication — only stated or strongly-implied; otherwise `null`.
- Per-run cap bounds cost and is logged when tripped.
- UPDATE + `fact_revision` per fact in a transaction; one fact's failure doesn't drop others.

## Testing (TDD; injected `model_call`)

- **Resolver:** explicit "in force since 2019" → `stage=in_force, stated`; implied "the law
  governs…" → `in_force, inferred`; no stage info → `null` (unchanged); model error →
  unchanged.
- **Storage:** inferred → `qualifier_provenance_json {stage: inferred}`, confidence downgraded,
  admission stays `provisional`, `fact_revision` written.
- **Catalog:** `compile_property_catalog` marks required qualifiers `(REQUIRED)`.
- **Completeness:** a fact with an inferred required qualifier → `assess_property_status`
  returns `resolved`; the targeted-axis directive is emitted only when `missing_qualifier`
  persists.
- **Graph:** `extract_facts → resolve_required_qualifiers → route_after_extract` wiring;
  graph-identity snapshot updated.
- **Idempotency:** resolver on an already-resolved fact is a no-op.
- **End-to-end verification:** a targeted harness run confirming `data_protection_law` lands
  `stage=in_force (inferred)`.

## Out of scope

- Cross-source qualifier evidence (resolve a qualifier from a different source than the value).
- Upgrading an already-inferred qualifier to explicit via a dedicated research round.
- The gap-loop hard cap for very large profiles (the deferred second sub-project).
