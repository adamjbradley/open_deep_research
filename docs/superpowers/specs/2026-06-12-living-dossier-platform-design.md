# Living Dossier Platform — Vision & Principles

**Date:** 2026-06-12
**Layer:** Vision + Principles (spec-driven development)
**Status:** v6 — reframed as a *fact base over the physical world* (entity instances and their
properties) during Feature-Spec dialogue; pending re-review of the reframe.
**Topic:** How the Open Deep Research platform should work for domain researchers

> **Revision history**
> - **v1** — initial vision from brainstorming dialogue.
> - **v2** — round-1 review (5 reviewers). Resolved three forks: workflow surfaces deferred;
>   prose+claims dual-canonical; confidence-gated ingestion.
> - **v3** — round-2 review (5 reviewers, 2 YES / 3 NO) found the v2 fixes collided. Resolutions
>   now baked in: (1) **degraded synthesis** — reports fall back to provisional claims *with
>   explicit caveats* when trusted knowledge is thin, fixing the empty-first-report / "purgatory"
>   dead-end; (2) **a concrete promotion rule** — promote on source-trust + no-open-contradiction,
>   corroboration explicitly *not* a promotion input until independence detection exists; (3) a
>   **read-only dossier view** (`dossier show <subject>`) pulled into v1 so conflict/confidence/
>   history are visible (interactive *adjudication* stays deferred); plus a reframed data model
>   (immutable run reports + accumulating claim base + on-demand dossier view) that fixes the
>   "dual-canonical / canonical-state undefined" problem.
> - **v4** — round-3 convergence check (3 ADVANCE / 2 minor). Precision fixes: run reports are
>   immutable historical snapshots (current conflicts live in the dossier view, not retro-injected
>   into past reports); rendering contract scoped to claim-derived rendering; "gated *promotion*"
>   not "gated ingestion"; honest flag that the promotion rule's "no open contradiction" clause
>   depends on contradiction-detection robustness; success metric de-tautologized.
> - **v5** — round-4 final check (4 ADVANCE / 1 minor). Fixed the sole objection: corrected P7's
>   claim that degraded-synthesis caveats backstop *over-promotion* — they don't (an over-promoted
>   claim renders as trusted/uncaveated); over-promotion is backstopped only by audit and later
>   detection→demotion, while caveats cover the distinct degraded-synthesis case. **Converged.**
> - **v6** — Feature-Spec dialogue revealed the product is a **fact base over the physical world**
>   (real-world entity *instances* and their measurable *properties*), not a literature-synthesis
>   tool. Went back to revise this layer (per the methodology's go-back protocol). A claim is now a
>   **fact triple** (instance → property → value, with unit + provenance) — *more* atomic than
>   prose-derived claims, which **lowers** the round-1 extraction-quality risk. A conflict is a
>   **value discrepancy**; the dossier view is a **structured fact table**. Competitive wedge
>   reframed (vs. Wikidata/knowledge-graphs, not Elicit). Pending re-review.

---

## 1. Vision

Open Deep Research becomes a platform for **building a living fact base about the physical world**:
an accumulating, provenance-bearing record of **real-world entities and their measurable properties**
— the population of a country, the half-life of a drug, the reported accuracy of a method — gathered
by automated research and held to scrutiny. It answers verifiable questions of fact, not scholarly
argument.

The product is the accumulating knowledge, held as three linked things: an **accumulating fact base**
(the machine-auditable layer — entity instances, properties, provenanced values), **immutable per-run
reports** (the source-grounded prose a run produced), and an on-demand **dossier view** that renders
the current factual state of an entity.

Each session deepens a body of knowledge the researcher returns to. We are honest that the payoff is
*conditional* (§4, §8): accumulation without quality control produces volume, not value — so the
platform's job is as much about *gating, surfacing disagreement between sources, and aging facts* as
about gathering them.

## 2. Who it's for

**Domain researchers** — people who need a dossier that holds up to scrutiny: cited, auditable,
explicit about uncertainty and disagreement. (Beachhead persona is deliberately unsettled at the
vision layer and must be named before the Feature Spec — see deferred doc.)

The engine is **domain-adaptable**, configured per deployment via a **domain profile**: vocabulary,
trusted-source priors, *and* the field's evidence expectations. Acknowledged limit: domains differ
in more than vocabulary, so "configuration-only" specialization is aspirational; the first domain
tuned against will shape defaults. **Commitment:** domain-specific assumptions live *only* in the
profile (data), never hard-coded in the engine, so bias is inspectable and revisable at one seam.

## 3. The core job

> Gather and maintain a living fact base about real-world entities — for each entity instance, an
> accumulating set of provenanced property values, the conflicts between sources, and the history of
> how each fact changed; rendered as a current dossier view, with the run reports behind it.

Success is measured by the quality and groundedness of the accumulated facts, not by any single
report. **Operationalized (v1 targets, to refine in Feature Spec):** the share of an entity's facts
that are *trusted* (vs. provisional) and whether it grows across runs; the share of facts backed by a
profile-trusted source; conflicts between sources surfaced rather than silently merged; stale facts
flagged rather than served as current. These distinguish a *living fact base* from a growing
*search history*.

## 4. Principles

### P1 — Trust through traceability and gating, audited over time
Research **auto-merges** into the claim base with **no human approval gate on ingestion** — but it
enters as **provisional** (P7). This is not a hidden approval gate: ingestion is unconditional; the
gate is an *automatic promotion policy* deciding what counts as established, not a human bottleneck.
Trust rests on **traceability** (P3) plus the **provisional→trusted policy** (P7).

We correct a v1 overstatement: *the cost of being wrong is not automatically "low."* A wrong claim
can mislead if presented as established. Defenses: (a) gating keeps unpromoted claims out of
*authoritative* synthesis; (b) degraded synthesis (P7) still uses provisional claims but *marks them
as such*; (c) after-the-fact audit. The platform's value depends on that audit happening; where it
doesn't, provisional claims remain visibly unverified rather than silently trusted.

### P2 — Knowledge lives as a fact base, run reports, and a dossier view (linked)
A research run produces an **immutable run report** (source-grounded prose, never rewritten) *and* a
set of **facts** extracted from it. A fact is a **triple — entity instance → property → value** —
with a unit where applicable and full provenance. Facts **accumulate** across runs into the fact
base. A **dossier view** renders the *current* factual state of an entity from the fact base on demand.

These are linked, not ranked: run reports are the citable human record of *what a run found*; the
fact base is the machine-auditable layer enabling provenance, confidence, conflict, and history; the
dossier view is the current factual picture. We explicitly **do not** claim a value is "the truth"
nor that facts can *regenerate* a run report — extraction is lossy, so a fact-derived rendering is a
*view*, not a reconstruction of the original prose. Facts are recorded, provenanced **assertions of
value**.

**The fact framing lowers a key risk.** Round-1 review flagged claim extraction (decomposing prose
into atomic claims) as the single biggest threat. A *fact triple* is far more atomic and bounded than
a free-form claim — "extract (instance, property, value, unit, source)" is a tighter, more verifiable
target than "extract the atomic propositions." Extraction quality remains the top Architecture risk,
but the surface is narrower and more checkable than the original framing.

### P3 — Every claim carries its provenance
Each claim records which **source(s)** it derives from, **when**, and **which run**. (Reality check:
today's pipeline only scrapes URLs from prose; binding a claim to the *specific* evidence for it is
real work, deferred to Architecture. The principle stands; the mechanism is not assumed solved.)

### P4 — Source trust and fact confidence are distinct axes — and both are heuristic
- **Source trust** is a domain-profile prior, **not uniform per source** (a source can be
  authoritative for one property, unreliable for another — a statistics agency on population, not on
  pharmacology). It is a prior, not a verdict.
- **Fact confidence** is a *computed, heuristic* property from supporting-source trust, whether other
  sources agree on the *value*, known conflicts, and (as a *weak, non-authoritative* signal only)
  corroboration count.

**Honest limitation:** agreement-on-value still **cannot detect source independence** (syndication,
churnalism, shared upstream datasets — e.g. five sites all restating one census figure). Corroboration
is therefore *displayed* but is **not** an input to trust *promotion* (P7) until independence detection
exists (§8). We do not claim corroboration as an anti-gaming defense.

### P5 — Disagreement is information; the platform never silently picks a winner
When new research yields a **different value for the same entity-property** (e.g. France population
68.2M from the UN vs. 67.9M from the World Bank), the platform **records both and marks the conflict**,
and **surfaces it in the current state of the entity** — the dossier view. It never auto-arbitrates a
winner or silently averages values. (A *run report* is an immutable snapshot of what one run found and the conflicts known *at
that time*; newly discovered conflicts surface in the dossier view, which renders current state — we
do not rewrite past reports. P2.) Crucially, gating does not silence disagreement: a contradicting
claim is *surfaced* even while provisional — "excluded from synthesis" (P7) means **not asserted as
established**, never **made invisible**. Interactive *adjudication* (resolve/override/triage) is
deferred (§6); the read-only dossier view *shows* conflicts in v1.

### P6 — Every fact has a history (data-model commitment)
Each fact has an **append-only revision history**: what the value changed to, when, the cause (new
run, gate promotion/demotion, conflict, staleness), and the why. Recorded and machine-queryable in
v1; surfaced read-only in the dossier view.

**The fact framing largely tames the round-1 "claim identity" risk.** A fact is keyed by
**(instance, property)**, so a new run's value for *France → population* attaches to the same fact as
a revision — no fuzzy proposition-level entity resolution required. The residual identity work is
narrower: **resolving entity instances** (is "USA" = "United States"?) and **aligning property names**
(is "pop." the *population* property?) — real, but far more bounded than the original problem, and
deferred to Architecture. **Extraction non-determinism** (does a re-extracted value differ because the
world changed or because the extractor drifted?) remains a deferred concern.

### P7 — Provisional by default; trusted by a defined rule; degraded synthesis when thin
Every newly extracted claim enters **provisional**. It is promoted to **trusted** by an automatic,
domain-profile-policy rule: **the supporting source(s) meet the profile's trust bar AND there is no
open contradiction.** (Corroboration is *not* a promotion input in v1 — see P4.) Promotion/demotion
is automatic by policy because interactive override is deferred; demotion occurs when a contradiction
opens against a previously-trusted claim.

**Honest dependency:** the "no open contradiction" clause is only as good as contradiction detection,
which P5 already requires for v1 (conflicts are first-class) but whose *robustness* is bounded by
claim-identity resolution (deferred, §9). Weak detection risks **over-promotion** — a real conflict
goes unseen and the claim is rendered as *trusted*, hence uncaveated. The backstops for *that* failure
are **after-the-fact audit** and later contradiction detection that **demotes** the claim; caveats do
*not* help here, because the system wrongly believes the claim is established. Caveats backstop a
*different* failure — the degraded-synthesis path, where a thin trusted base forces openly-provisional
claims into a report and they must be marked as such. Both backstops are necessary, not optional
polish. The caveat-generation mechanism itself is deferred (§9); the *commitment* that provisional
content is always marked as such is firm.

**Report synthesis prefers trusted claims, but degrades gracefully:** when trusted claims are
insufficient (every brand-new subject; heavily-contested topics), synthesis **falls back to
provisional claims with explicit, machine-generated caveats** rather than producing an empty report.
The dossier therefore has value from session one, while still distinguishing established from
provisional knowledge. This resolves the cold-start / "purgatory" dead-end identified in review.

**Admission status (`provisional`/`trusted`) and lifecycle status (`current`/`stale`/`superseded`)
are separate axes** — a trusted claim can become stale; a provisional claim can be current. (Earning
trust may require *re-research* to confirm; whether the platform actively triggers that, versus
waiting for the next user query, is a deferred mechanism — the "flywheel motor" question, §8.)

## 5. The model (vision-level)

- **Entity type** — a class of real-world thing the deployment tracks (*country*, *drug*, *method*).
  Defines the **properties** worth gathering. Part of the domain profile.
- **Entity instance** — a specific member of a type (*France*, *aspirin*, *ResNet-50*). **This is the
  subject/dossier** (keyed by slug; the existing `subjects` notion, now typed).
- **Property** — a named attribute of a type (*population*, *half-life*, *reported accuracy*), with an
  expected value kind / unit. Properties may be predefined by the profile *and* discovered by research
  (open question — §9).
- **Fact** — a provenanced **(instance, property, value)** triple with unit where applicable. Carries
  source(s), origin run, admission status, lifecycle status, and computed confidence. Retains the
  qualifiers (as-of date, scope/conditions) needed to be meaningful.
- **Fact revision** — append-only history entry: what/when/cause/why (P6).
- **Run report** — immutable source-grounded prose for a run; never rewritten; linked to its facts.
- **Source** — external reference with a domain-profile trust prior (P4).
- **Conflict** — first-class link between facts on the same instance+property with differing values,
  `open`/`resolved` (P5).
- **Resolution** — adjudication record (deferred interactive workflow): chosen value, rationale,
  who, when.
- **Dossier view** — an on-demand rendering of an entity's *current* factual state: trusted facts,
  provisional facts (caveated), open conflicts, confidence and staleness — a structured fact table.
  Read-only in v1 (`dossier show <instance>`).

### Pipeline implication
A **fact-extraction step** after `compress_research` (within/around `persist_research`): an LLM
structured-output pass decomposes a run's citation-bearing findings into **(instance, property,
value, unit, source)** facts. Preserves the core invariant — *the graph owns the agentic loop*.
**Named risks:** extraction quality is still the top risk, but the fact-triple target is narrower
and more checkable than free-form claims (P2); structured output remains brittle on the Gemini/Codex
backends (which coerce JSON envelopes, per CLAUDE.md) — and a fixed fact schema is *easier* to coerce
than open-ended claims. Both deferred to Architecture.

### Rendering contract
A data-model invariant cannot by itself guarantee a presentation invariant. Therefore the renderer
has explicit obligations: **never silently flatten a conflict, never drop a confidence/admission
tier, never present a provisional or contested claim as established.** This contract governs all
*claim-derived* rendering — the dossier view, and the synthesis written into a run report *at run
time* — through one canonical render path, so the contract holds in one place. (A stored run report
is thereafter an immutable artifact, displayed as-written; it is not re-rendered against later state.)

## 6. Scope and non-goals

**In scope (v1):**
- The **entity-type / instance / property** schema and the **fact base + run report + dossier view**
  data model with provenance, confidence, conflicts, history (P2–P6).
- The **fact-extraction** pipeline step.
- **Hybrid source space** — scholarly sources (for authoritative facts) *and* the open web (for
  currency/coverage), distinguished by source-trust tiers (P4). This adds a scholarly-source
  integration on top of today's open-web search.
- **Confidence-gated promotion** (ingestion is unconditional; *promotion* is gated) with the defined
  promotion rule and **degraded synthesis** (P7).
- **Conflict surfacing** (value discrepancies) inline in rendered output.
- A **read-only dossier view** (`dossier show <instance>`) — the structured fact table that makes
  conflict/confidence/history visible and the §7 wedge testable in session one.
- Per-deployment **domain profile** (entity types, properties, trusted-source priors).

**Out of scope / explicit non-goals (v1):**
- **No interactive workflow UI.** No conflict-adjudication / resolve-override / triage-queue surface,
  no fact-editing UI. The data is captured (P5/P6) and *shown* read-only; *acting* on it interactively
  is deferred. (Narrower than v2's blanket "no UI": read-only rendering is in scope; interactive
  mutation of the fact base is not.)
- No multi-user / collaboration / shared fact bases — single trusted researcher per deployment.
- No backend re-architecture beyond what fact extraction and the scholarly-source integration require
  (flagged: structured-output coercion and a new source connector are real, bounded work).

## 7. Competitive reality (why this, not the incumbents)

The fact-base reframe *strengthens* the wedge — we no longer fight Elicit/NotebookLM on literature
synthesis. The honest wedge: **an automatically-built, provenance-and-conflict-aware fact base over
real-world entities — like a personal, auto-researched Wikidata that keeps, and shows, the
disagreement and history that structured knowledge bases throw away.**

- **Wikidata / Wikipedia** — broad structured facts, but manually curated, single "consensus" value
  per property (disagreement is hidden in talk pages / revision logs), and not built on demand for
  *your* entity set. This keeps competing source values side by side, with provenance and confidence.
- **Knowledge graphs / Diffbot-style extractors** — extract facts at scale but are hosted, opaque on
  provenance per value, and don't preserve per-fact *conflict* or *confidence history*.
- **ChatGPT/Claude (+ search/memory)** — will state a fact fast, but ungrounded, no per-value
  provenance, no conflict model, no local ownership, no accumulation you can audit.
- **Manual spreadsheets / Zotero** — what many fact-gatherers actually use today: total control, zero
  automation, no conflict/confidence tracking. This automates the gathering while keeping the rigor.

**Stated honestly:** the differentiator is the *visible per-fact provenance + cross-source conflict +
confidence/history* layer over an auto-built fact base. If that isn't compelling enough for a concrete
target persona to switch, the vision is weaker than it looks — which is why the read-only dossier view
is in v1 (to make the wedge testable, not asserted) and why naming the beachhead persona is a
Feature-Spec gate.

## 8. Required-coverage considerations

- **Epistemic safety:** primary risk is presenting unverified/disputed claims as settled. Defenses:
  P7 gating + degraded-synthesis caveats, P5 surfacing, P4 confidence honesty, the rendering
  contract. Residual: faithful citation of a *misleading* source and synthesis distortion are not
  caught by provenance — Architecture to consider sampling/quarantine and a high-stakes-domain gate.
- **Inclusion / bias:** domain-profile priors encode "whose sources count" — explicit, inspectable,
  revisable, and confined to the profile (P2 commitment). Cascading invalidation on a trust change
  needs defined recompute behavior — deferred.
- **Legal & compliance:** **append-only history vs. right-to-erasure** reconciled at principle level
  via **redaction/tombstoning** (remove protected content, preserve history's shape; propagate to
  rendered artifacts and backups). Licensing, fair-use, derivative-work risk of extracted claims,
  sensitive-category data, access control — named obligations, deferred to Architecture.
- **Risk & exploitation:** confidence-gaming is *not* defended by corroboration (now excluded from
  promotion) or by auditability (a post-mortem). Independence detection and resistance to laundered
  corroboration are open Architecture problems. Deferred adjudication must later consider
  prompt-injection / accidental resolution.
- **Erosion over time:** temptations to silently auto-resolve conflicts (violates P5) or drop history
  (violates P6) are named to be resisted; unbounded growth of conflicts/history is real — GC,
  staleness/temporal-validity policy, and the promotion "flywheel" are deferred.
- **Economic viability:** extraction + contradiction checks add LLM passes; subscription/CLI backends
  make this incremental **but not free** (quotas, latency, provider-policy limits on automated use;
  O(N) contradiction-check scaling) — Architecture cost/throughput model.
- **Unknown unknowns:** semantic drift, near-duplicate explosion, operational recovery after
  extractor/prompt upgrades — captured in the deferred doc.

## 9. Open questions (deferred to Architecture)

See `2026-06-12-deferred-living-dossier-platform.md`. Headlines: **entity-instance resolution + property
alignment** (the narrowed successor to "claim identity"); **property definition** — predefined per
type vs. discovered by research vs. hybrid; fact-extraction quality/non-determinism; provenance
binding; **scholarly-source integration** (which connectors; how value+unit are parsed); independence/
echo detection; the promotion "flywheel" (active re-research vs. passive); degraded-synthesis caveat
generation; canonical dossier-view (fact-table) rendering rules; temporal-validity/staleness (facts
about the world change); redaction-vs-append-only; structured-output brittleness; O(N) conflict-check
scaling and recompute-on-trust-change; beachhead persona; success-metric refinement.

---

*Status: v6 reframes the platform as a **fact base over the physical world**, going back to the Vision
layer (per the methodology's go-back protocol) after the Feature-Spec dialogue surfaced it. The
principles survived the reframe intact and the fact-triple model **reduces** prior top risks
(extraction, claim identity). Next step: a re-review of the reframe, then resume the **Feature Spec**
layer (persona, v1 scope, user stories, success metrics).*
