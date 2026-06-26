# Design: required-qualifier resolution

**Status:** designed (brainstormed) — **revised after review round 1** (codex / claude / agy
feedback). First of two sub-projects from the gap-prioritization follow-up thread; the second
(gap-loop hard cap for very large profiles) is deferred.

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

A fact lands its value **and** its required qualifiers, via three levers in strict priority
order: **capture explicit → targeted research → bounded inference (marked, promotion-blocked)**.
The property then fully resolves instead of churning as `missing_qualifier`.

## Decisions (from brainstorming, revised after review round 1)

- **Bounded inference is allowed** for *required* qualifiers from clear contextual cues (a law
  that "applies"/"governs" → `stage=in_force`). Optional qualifiers keep the strict
  "explicit-only" rule.
- **Inference is the LAST resort, not the first** *(revised — review §P0-2)*. The original
  design inferred eagerly and let inference terminate the loop; review showed that, for the
  motivating legal property, inference would pre-empt the targeted primary-source research and
  settle for a low-trust inferred `in_force`. Revised: a missing required qualifier triggers a
  **targeted research attempt first**; inference fires only on a *later* round, once research
  has been attempted for that axis and the qualifier is still missing. This makes the priority
  order real (explicit → research → infer) rather than aspirational.
- **Inferred qualifiers are marked, and the marker has teeth** *(revised — review §P0-1)*. The
  live provenance signal is `qualifier_provenance_json`; `promotion.evaluate` reads it and
  **blocks promotion to `trusted`** of any fact whose required qualifier is inferred, so an
  inferred legal fact never renders as trusted. The fact's `confidence` is recorded lower for a
  future precedence project but is **explicitly inert in v1** (no consumer wired yet) — we don't
  claim a precedence behavior that doesn't exist.
- **Research is surgical:** when a required qualifier is missing, the gap round emits a
  *targeted sub-query* naming that specific axis + its enum options, preferring primary sources.
- **A post-extraction resolver** owns the inference, keeping bulk extraction lean.

## Architecture

A new node sits between extraction and the completeness/sufficiency check, mode-agnostic
(required qualifiers matter in facts-first mode too):

```
… → extract_facts → resolve_required_qualifiers (NEW) → route_after_extract
                                                          → assess_completeness | assess_sufficiency | persist_research
```

Verified against `main`: the `extract_facts → route_after_extract` edge and its three targets
exist (`deep_researcher.py`); the resolver inserts on that edge so it runs in both whole-profile
and facts-first modes and is a no-op (no missing required qualifiers) at zero cost.

### End-to-end flow (research-preferred)

1. `extract_facts` does bulk per-source extraction as today, capturing explicitly-stated
   qualifiers and ingesting facts.
2. **`resolve_required_qualifiers`** finds facts with a value but a missing required qualifier:
   - **Stated:** if the fact's evidence (joined from the `evidence` table, see §3) explicitly
     states the qualifier, capture it (`basis = stated`).
   - **Defer-to-research:** if not stated **and** no targeted research has yet been attempted for
     this axis, leave it `missing_qualifier` (do **not** infer yet).
   - **Infer (last resort):** if not stated **and** targeted research was already attempted for
     this axis on a prior round, infer from the evidence (`basis = inferred`, promotion-blocked),
     so the loop can terminate.
3. `assess_completeness` / `assess_sufficiency` re-evaluate. A required qualifier now present
   (stated or inferred) → **resolved**. For a still-`missing_qualifier` property whose axis has
   not yet been researched, it emits a **targeted sub-query** (§4) and records the axis as
   research-attempted.
4. The next round fetches a primary source; bulk extraction captures the qualifier **explicitly**
   (the common path for a cross-source qualifier — see §3). If it still doesn't, the resolver
   infers it on that round as the last resort.

Because the resolver runs *before* the completeness check, whichever lever resolves the axis is
reflected before the gap-loop's bail-out decision — inference (when it is finally allowed) can't
be pre-empted by the bail-out.

## Components

### 1. Catalog: mark required qualifiers (single-sourced)

`compile_property_catalog` appends `(REQUIRED)` to required qualifiers so both the bulk extractor
and the resolver know which axes are mandatory, e.g.:

```
- data_protection_law (boolean): … | qualifiers: stage=[enacted, in_force] (REQUIRED); scope=[comprehensive, sectoral]
```

The bulk-extractor prompt path and the resolver consume the **same** compiled string — "required"
is asserted once, not duplicated (review §P2-8).

### 2. `resolve_required_qualifiers` node

- **Selects** the run's facts with a value but ≥1 absent required qualifier (the same condition
  the completeness ledger uses for `missing_qualifier`), **excluding** facts already attempted
  with a `null` result whose evidence hasn't changed (review §P2-5 — avoids re-calling the model
  on identical input every round).
- **For each fact + missing axis**, applies stated → defer → infer (per the flow above) via an
  **injected `model_call`** (testable without a live model; routable to a stronger model — the
  task is tiny and accuracy-sensitive):
  > Property `<name>` (value `<value>`) for `<instance>`. Evidence: "`<evidence_span>`". The
  > required qualifier `<qualifier>` must be one of `<enum>`. If the evidence **explicitly
  > states** it → `{value, basis: "stated"}`; if it **strongly implies** it →
  > `{value, basis: "inferred"}`; if neither → `{value: null}`.
  > *(The node only requests an inferred answer when research has already been attempted; before
  > that it asks for `stated`-only and treats implied as `null` so the axis routes to research.)*
- **Best-effort:** a model error or `null` leaves the fact unchanged; never fails the run.
- **Bounded + logged:** a per-run cap (`max_qualifier_resolutions`, default mirroring
  `max_fact_rounds`) limits resolver calls; capped-out facts stay `missing_qualifier` → targeted
  research — a deliberate graceful-degradation path, not a silent drop (review §P2-6).
- **Idempotent:** facts with no missing required qualifier, and attempted-`null` facts with
  unchanged evidence, are skipped — safe to run every gap round.
- **Observability:** logs a per-run breakdown of `stated / inferred / null` counts. A high
  inferred-share (especially on legal properties) is a red flag, not a success signal
  (review §P2-observability).

### 3. Evidence-span source + cross-source qualifiers

The persisted `Fact` model does not store the evidence span; it lives in the `evidence` table
(`evidence.quoted_span`, keyed by `fact_id`). The resolver therefore **joins `fact` → `evidence`
by `fact_id`** to read the span it infers from (review §P1-4 / codex C3).

A required qualifier is often missing precisely because it lives in a *different* source than the
value (the value in a qualitative source A; the enactment date in statute source B). **This is
handled by research-preferred, not by cross-source span-reading:** the targeted sub-query fetches
the primary source, and ordinary bulk extraction on that source captures the qualifier
explicitly. The resolver only ever reads its *own* fact's evidence (for the last-resort
inference). Cross-source resolver-reading therefore stays out of scope **without being
load-bearing** — research-preferred covers the cross-source case (resolves review §P1-4's
concern that the deferred enhancement was load-bearing).

### 4. Research: targeted qualifier sub-query (with a real handoff)

The current completeness API returns one status string per property; it does not carry *which*
qualifier is missing or its enum options (codex C1). This spec adds a helper that derives, from
the grouped rows + ledger, the specific missing required-qualifier axes per property
(`{property: [{qualifier, enum_options}]}`). `assess_completeness` consumes it to emit an
axis-specific directive into the gap brief instead of the bare `<name> (missing_qualifier)`:

> `data_protection_law`: the value is known, but its required `stage` (enacted vs in_force) is
> unconfirmed — find a **primary/official** source (the statute, act, or regulator) stating the
> enactment or in-force date.

It also records the axis as **research-attempted** (threaded in state, e.g.
`qualifier_research_attempted: set of (property, qualifier)`), which is what gates the resolver's
last-resort inference on the next round.

### 5. Promotion: block promotion of inferred required qualifiers

`promotion.evaluate` currently re-derives `admission` from conflict-eligibility alone, reading
neither `confidence` nor provenance — so "keep admission = provisional" is a no-op and an
inferred fact would still promote to `trusted` (review §P0-1 / §P0-3). This spec changes
`promotion.evaluate` to **read `qualifier_provenance_json` and refuse to promote a fact whose
required qualifier is inferred**. That is the live mechanism giving the inferred marker teeth; a
later explicit, promotable fact for the same axis supersedes the inferred one through normal
promotion.

### 6. Storage: provenance marker (live) + confidence (recorded, inert in v1)

The resolver runs post-ingest, so it **updates** the existing fact row and writes a
`fact_revision` row (audit trail):

- `qualifiers_json` gains the resolved value.
- A new nullable `qualifier_provenance_json` column on the `fact` table (schema migration —
  current schema is v10, this is v11) records inferred axes, e.g. `{"stage": "inferred"}`
  (stated qualifiers are the default, unmarked). **This is the live signal**, read by
  `promotion.evaluate` (§5) and available to rendering.
- The fact's `confidence` (existing column) is set lower for an inferred required qualifier. It
  is recorded for a **future** precedence project; nothing consumes it in v1, and the spec does
  not claim otherwise (review §P0-1). Wiring confidence into conflict/promotion/render precedence
  is explicitly out of scope here.

### 7. Completeness integration

Because the resolver runs *before* the completeness check, the ledger simply sees the filled
qualifier and returns `resolved`. An inferred qualifier satisfies the required-qualifier check
for **loop termination**; its promotion-block (§5) and provenance marker (§6) keep it from
rendering as trusted. The completeness changes are: the axis-aware gap directive (§4) and the
research-attempted bookkeeping that gates inference.

## Error handling

- Resolver errors/timeouts/`null` → fact unchanged, non-fatal (mirrors `extract_facts`).
- No fabrication — only stated, or strongly-implied *after* research was attempted; otherwise
  `null`.
- Per-run cap bounds cost; capped-out facts degrade gracefully to `missing_qualifier` → targeted
  research (a named feature, not an afterthought).
- Attempted-`null` facts are not re-attempted until new evidence lands (no within-run flap).
- UPDATE + `fact_revision` per fact in a transaction; one fact's failure doesn't drop others.

## Testing (TDD; injected `model_call`)

- **Resolver — stated:** evidence explicitly states "in force since 2019" → `stage=in_force,
  stated`.
- **Resolver — defer:** implied cue but no prior research for the axis → `null` (left for
  research, NOT inferred).
- **Resolver — infer (last resort):** implied cue **and** research already attempted →
  `in_force, inferred`.
- **Resolver — none:** no stage cue → `null` (unchanged); model error → unchanged.
- **Re-attempt:** an attempted-`null` fact with unchanged evidence is skipped on the next round.
- **Storage:** inferred → `qualifier_provenance_json {stage: inferred}`, confidence set lower,
  `fact_revision` written.
- **Promotion:** a fact with an inferred required qualifier is **not** promoted to `trusted`; an
  equivalent fact with the qualifier *stated* is promotable.
- **Catalog:** `compile_property_catalog` marks required qualifiers `(REQUIRED)`, single-sourced
  between bulk extractor and resolver.
- **Completeness handoff:** the axis-aware directive names the specific missing qualifier + enum
  options; the axis is recorded research-attempted.
- **Graph:** `extract_facts → resolve_required_qualifiers → route_after_extract` wiring;
  graph-identity snapshot updated.
- **End-to-end verification:** assert the property **resolves by either lever** (not that
  inference specifically wins — review §P2-7); *if* the result is inferred, assert provenance +
  promotion-block are stamped. Avoids baking a non-deterministic model outcome into the success
  criterion.

## Out of scope

- Wiring `confidence` into conflict/promotion/render *precedence* (a future project; v1 records
  it inert and uses the provenance marker + promotion-block for teeth).
- Cross-source qualifier resolution by the resolver reading a *different* source's span (covered
  instead by research-preferred feeding bulk extraction).
- The gap-loop hard cap for very large profiles (the deferred second sub-project).
