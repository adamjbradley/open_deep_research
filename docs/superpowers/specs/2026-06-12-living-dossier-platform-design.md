# Living Dossier Platform — Vision & Principles

**Date:** 2026-06-12
**Layer:** Vision + Principles (spec-driven development)
**Status:** v8 — round-6 re-convergence check (4 ADVANCE / 1 minor + gemini pending). Fixed the one
objection: provenance is part of fact identity, so a qualified key holds multiple competing
source-assertions (the conflict) while the dossier presents one resolved value. Reframe converged.
**Topic:** How the Open Deep Research platform should work for domain researchers

> **Revision history**
> - **v1** — initial vision from brainstorming dialogue.
> - **v2** — round-1 review (5 reviewers). Resolved three forks: workflow surfaces deferred;
>   prose+claims dual-canonical; confidence-gated ingestion.
> - **v3** — round-2 review (2 YES / 3 NO): v2 fixes collided. Baked in degraded synthesis, a
>   concrete promotion rule, a read-only dossier view, and a reframed report/claim/view data model.
> - **v4** — round-3 convergence check (3 ADVANCE / 2 minor). Precision fixes (immutable reports,
>   rendering contract, "gated promotion", dependency flag, de-tautologized metric).
> - **v5** — round-4 final check (4 ADVANCE / 1 minor). Fixed caveats-vs-over-promotion. Converged.
> - **v6** — Feature-Spec dialogue reframed the product as a **fact base** (entity instances and
>   measurable properties). Went back to the Vision layer (go-back protocol). *Claimed* the reframe
>   lowered extraction + claim-identity risk.
> - **v7** — round-5 re-review (5 reviewers, **5 ANOTHER ROUND**) found v6 under-modeled facts.
>   Deepened: **(1) facts are qualifier-aware** — identity is (instance, property, qualifiers such as
>   as-of date / scope / method); a conflict is differing values *with the same qualifiers*, not a raw
>   value difference (so France pop 2021 vs 2023 is two facts, not a conflict). **(2) Retracted the v6
>   risk-reduction overclaims** — fact triples narrow the *schema*, not the *semantic-fidelity* risk;
>   the qualifiers carry the hard part, so claim-identity is *relocated, not tamed*. **(3) Broadened
>   the framing** from "physical world" to *attributed facts about entities* (measured, derived,
>   methodological, institutional). **(4)** Multi-valued / time-series / relationship facts and
>   value-equality/unit-normalization named as deferred. Persona to be anchored on a **high-stakes**
>   use where cross-source conflict matters.
> - **v8** — round-6 re-convergence check (4 ADVANCE / 1 minor). Fixed codex's catch: v7's "single
>   value per qualified key" contradicted the conflict model. Resolution: **provenance is part of fact
>   identity** — each fact is one source's assertion, so a qualified key carries multiple competing
>   source-assertions (which *is* the conflict), while the dossier view presents one *resolved current*
>   value. Clarified revision (one source over time) vs. conflict (different sources), and intrinsic
>   multi-valued properties vs. cross-source disagreement. Carried a Feature-Spec tension: high-stakes
>   users may need to *act* on conflicts, but v1 is read-only. **Reframe converged.**

---

## 1. Vision

Open Deep Research becomes a platform for **building a living fact base about entities** — an
accumulating, provenance-bearing record of **things in the world and their attributes**: the
population of a country, the half-life of a drug, the reported accuracy of a method on a named
benchmark, the regulatory status of a product in a jurisdiction. Facts are gathered by automated
research and held to scrutiny. The platform answers **attributed questions of fact** — where the
value, its source, its conditions, and any disagreement all travel together — rather than producing
open-ended argument or synthesis prose as its primary product.

(Scope boundary, stated honestly: this is deliberately *not* a tool for synthesizing mechanisms,
causal theories, or qualitative argument. That is a real capability the reframe gives up — a
worthwhile trade only if the target user's core need is attributed facts. We test that with the
persona, §2.)

The product is the accumulating knowledge, held as three linked things: an **accumulating fact base**
(the machine-auditable layer — entity instances, properties, qualified+provenanced values),
**immutable per-run reports** (the source-grounded prose a run produced), and an on-demand **dossier
view** that renders the current factual state of an entity.

Each session deepens a body of knowledge the researcher returns to. We are honest that the payoff is
*conditional* (§4, §8): accumulation without quality control produces volume, not value — so the
platform's job is as much about *gating, surfacing disagreement between sources, and aging facts* as
about gathering them.

## 2. Who it's for

**Domain researchers** for whom **cross-source factual disagreement actually matters** — the
high-stakes slice where "what's the value, who says so, and do sources agree?" is worth real effort:
regulatory / compliance, due-diligence, scientific reference-data, competitive/market intelligence.
This framing is deliberate: round-5 review warned that low-stakes fact lookup is a commodity LLMs
give away free, so the beachhead must be a user for whom provenance and conflict are the point. The
concrete persona is the first Feature-Spec gate (deferred doc), chosen through this lens.

The engine is **domain-adaptable**, configured per deployment via a **domain profile**: entity types,
their properties (with expected value kinds/units), trusted-source priors, and evidence expectations.
Acknowledged limit: domains differ in more than vocabulary, so "configuration-only" specialization is
aspirational; the first domain tuned against will shape defaults. **Commitment:** domain-specific
assumptions live *only* in the profile (data), never hard-coded in the engine, so bias is inspectable
and revisable at one seam.

## 3. The core job

> Gather and maintain a living fact base about entities — for each entity instance, an accumulating
> set of qualified, provenanced property values, the conflicts between sources, and the history of how
> each fact changed; rendered as a current dossier view, with the run reports behind it.

Success is measured by the quality and groundedness of the accumulated facts, not by any single
report. **Operationalized (v1 targets, to refine in Feature Spec):** the share of an entity's facts
that are *trusted* (vs. provisional) and whether it grows across runs; the share of facts backed by a
profile-trusted source; genuine cross-source conflicts surfaced (not merged, not falsely manufactured
from differing qualifiers); stale facts flagged rather than served as current. These distinguish a
*living fact base* from a growing *search history*.

## 4. Principles

### P1 — Trust through traceability and gating, audited over time
Research **auto-merges** into the fact base with **no human approval gate on ingestion** — but it
enters as **provisional** (P7). This is not a hidden approval gate: ingestion is unconditional; the
gate is an *automatic promotion policy* deciding what counts as established, not a human bottleneck.
Trust rests on **traceability** (P3) plus the **provisional→trusted policy** (P7).

We correct a v1 overstatement: *the cost of being wrong is not automatically "low."* A wrong fact can
mislead if presented as established. Defenses: (a) gating keeps unpromoted facts out of *authoritative*
synthesis; (b) degraded synthesis (P7) still uses provisional facts but *marks them as such*;
(c) after-the-fact audit. The platform's value depends on that audit happening; where it doesn't,
provisional facts remain visibly unverified rather than silently trusted.

### P2 — Knowledge lives as a fact base, run reports, and a dossier view (linked)
A research run produces an **immutable run report** (source-grounded prose, never rewritten) *and* a
set of **facts** extracted from it. A fact is a **qualified triple — entity instance → property →
value** — carrying a unit where applicable, the **qualifiers (as-of date, scope, method, conditions)
that make the value meaningful**, and full provenance. Facts **accumulate** across runs into the fact
base. A **dossier view** renders the *current* factual state of an entity on demand.

These are linked, not ranked: run reports are the citable human record of *what a run found*; the
fact base is the machine-auditable layer; the dossier view is the current factual picture. We
explicitly **do not** claim a value is "the truth," nor that facts can *regenerate* a run report —
extraction is lossy, so a fact-derived rendering is a *view*. Facts are recorded, provenanced
**assertions of value under stated conditions**.

**Honest correction to v6.** v6 claimed the fact framing *lowers* the extraction and claim-identity
risks. Round-5 review refuted this and we accept the correction: a fact triple narrows the **output
schema** (a tighter, more checkable target than free-form propositions), but it does **not** lower the
**semantic-fidelity** risk — entity linking, unit conversion, capturing the right qualifiers and
as-of date, and binding to the specific evidence are exactly as hard as before, now living *inside*
the triple's qualifiers. Net: the grammar is cleaner; the hard part is relocated, not removed. The
real benefit is structural (a typed, queryable, conflict-comparable store), not reduced extraction
difficulty.

### P3 — Every fact carries its provenance
Each fact records which **source(s)** it derives from, **when**, and **which run**. (Reality check:
today's pipeline only scrapes URLs from prose; binding a fact to the *specific* evidence for its value
is real work, deferred to Architecture. The principle stands; the mechanism is not assumed solved.)

### P4 — Source trust and fact confidence are distinct axes — and both are heuristic
- **Source trust** is a domain-profile prior, **scoped per (type, property)** — a statistics agency is
  authoritative on population, not on pharmacology. It is a prior, not a verdict.
- **Fact confidence** is a *computed, heuristic* property from supporting-source trust, whether other
  sources agree on the *value under the same qualifiers*, known conflicts, and (as a *weak,
  non-authoritative* signal only) corroboration count.

**Honest limitation:** agreement-on-value still **cannot detect source independence** (syndication,
churnalism, shared upstream datasets — five sites restating one census figure). Corroboration is
therefore *displayed* but is **not** an input to trust *promotion* (P7) until independence detection
exists (§9). We do not claim corroboration as an anti-gaming defense.

### P5 — Disagreement is information; the platform never silently picks a winner
A **conflict** is when two sources give a **different value for the same entity, property, *and*
qualifiers** — e.g. France population *as of 2023* reported as 68.2M (UN) vs. 67.9M (World Bank).
Differing **qualifiers do not make a conflict**: France's population in 2021 vs. 2023, or a method's
accuracy on dataset A vs. B, are *distinct facts*, not a disagreement. Deciding "different value"
requires a typed **value-equality with tolerance and unit normalization** (68.2M = 68,200,000 ≠
67.9M; 5 mg/L vs 14 µmol/L must be normalized) — a primitive the reframe newly requires (deferred
mechanism, §9).

When a genuine conflict exists, the platform **records both and marks it**, and **surfaces it in the
dossier view**. It never auto-arbitrates a winner or silently averages. (A *run report* is an
immutable snapshot of what one run found; newly discovered conflicts surface in the dossier view, not
by rewriting past reports — P2.) Gating does not silence disagreement: a contesting value is surfaced
even while provisional — "excluded from synthesis" (P7) means **not asserted as established**, never
**made invisible**. Interactive *adjudication* (resolve/override/triage) is deferred (§6); the
read-only dossier view *shows* conflicts in v1.

### P6 — Every fact has a history (data-model commitment)
Each fact has an **append-only revision history**: what the value changed to, when, the cause (new
run, gate promotion/demotion, conflict, staleness), and the why. Recorded and machine-queryable in v1;
surfaced read-only in the dossier view. Because provenance is part of fact identity (§5), a revision
tracks how *one source's* asserted value evolves across runs (e.g. the UN restating France's
population); a *different* source's differing value is a **separate fact and a conflict** (P5), never
silently folded in as a revision — this is the replacement-vs-concurrent-disagreement distinction.

**Honest scope of the identity problem (correcting v6).** A fact is keyed by **(instance, property,
qualifiers)**. This gives history a real primary key, but it does **not** "tame" the round-1
identity problem — it *relocates* it into two genuinely hard sub-problems, deferred to Architecture:
**entity-instance resolution** ("USA" = "United States"?), and **property + qualifier alignment**
(is "pop." the *population* property? is "2023" the same as-of basis as "mid-2023 estimate"?).
Mis-aligning qualifiers either fabricates conflicts or collapses distinct facts. **Extraction
non-determinism** (a re-extracted value differing from drift, not from the world changing) remains a
deferred concern.

### P7 — Provisional by default; trusted by a defined rule; degraded synthesis when thin
Every newly extracted fact enters **provisional**. It is promoted to **trusted** by an automatic,
domain-profile-policy rule: **the supporting source(s) meet the profile's (type, property) trust bar
AND there is no open conflict for that (instance, property, qualifiers).** (Corroboration is *not* a
promotion input in v1 — see P4.) Promotion/demotion is automatic by policy because interactive
override is deferred; demotion occurs when a conflict opens against a previously-trusted fact.

**Honest dependency:** the "no open conflict" clause is only as good as conflict detection — which
needs the value-equality/unit-normalization primitive (P5) and qualifier alignment (P6), both
deferred. Qualifier alignment fails in *both* directions, and both are dangerous. **Under-matching** (lossy
extraction fails to see that two source-assertions share a key) makes conflicts artificially rare →
**systemic over-promotion** (a real conflict goes unseen and the fact renders as *trusted*, hence
uncaveated); the backstops are **after-the-fact audit** and later detection→**demotion**, *not*
caveats (the system wrongly believes it is established). **Over-matching** (treating distinct
qualifiers as the same) manufactures **false conflicts** that wrongly *block* sound facts from
promotion. This means the gating mandate (P1) is only as strong as deferred qualifier alignment —
named, not hidden; degraded-synthesis caveats cover only the distinct thin-trusted-base case. The caveat-generation mechanism is deferred (§9); the *commitment*
that provisional content is always marked is firm.

**Report synthesis prefers trusted facts, but degrades gracefully:** when trusted facts are
insufficient (brand-new entities; heavily-contested properties), synthesis **falls back to provisional
facts with explicit, machine-generated caveats** rather than producing nothing. The dossier has value
from session one while still distinguishing established from provisional knowledge. This resolves the
cold-start / "purgatory" dead-end. The **dossier view (fact table) is the centerpiece**; prose
synthesis is the secondary, source-grounded rendering.

**Admission status (`provisional`/`trusted`) and lifecycle status (`current`/`stale`/`superseded`)
are separate axes** — a trusted fact can become stale; a provisional fact can be current. A *newer
value under a newer as-of date* is a new current fact, not a conflict and not a demotion (P5).
(Whether the platform actively triggers re-research to refresh/earn trust, vs. waiting for the next
query, is the deferred "flywheel motor", §9.)

## 5. The model (vision-level)

- **Entity type** — a class the deployment tracks (*country*, *drug*, *method*). Defines the
  **properties** worth gathering. Part of the domain profile.
- **Entity instance** — a specific member (*France*, *aspirin*, *ResNet-50*). **The subject/dossier**
  (keyed by slug; today's `subjects`, now typed). Instance resolution is a deferred problem (P6, §9).
- **Property** — a named attribute (*population*, *half-life*, *reported accuracy*) with an expected
  value kind / unit, and the **qualifiers** it is parameterized by (as-of date, scope, method).
  Properties may be profile-predefined *and/or* discovered by research (open question — §9).
- **Fact** — *one source's* provenanced assertion: **(instance, property, qualifiers) → value
  (+unit)**, identified including its **source/run** (provenance is part of identity, per P3). So
  several sources asserting values for the *same* (instance, property, qualifiers) produce **several
  facts** — and when their values are unequal (under P5's value-equality), that coexistence **is the
  conflict**. The qualified key is therefore *not* single-valued in storage; what is single is the
  **resolved current value** the dossier view presents per key (a trusted value, or "in conflict").
  Carries admission status, lifecycle status, computed confidence. (**Intrinsically multi-valued
  properties** — a country's official *languages* — plus **time-series and relationship facts** are a
  separate, deferred concern, §9, not to be confused with cross-source conflict on a single-valued
  property.)
- **Fact revision** — append-only history entry: what/when/cause/why (P6).
- **Run report** — immutable source-grounded prose for a run; never rewritten; linked to its facts.
- **Source** — external reference with a (type, property)-scoped trust prior (P4).
- **Conflict** — first-class link among the (≥2) facts on the same (instance, property, qualifiers)
  whose values are unequal under tolerance/unit-normalization, `open`/`resolved` (P5). Conflict is a
  relation *between source-assertions*, which is why provenance is part of fact identity above.
- **Resolution** — adjudication record (deferred interactive workflow): chosen value, rationale,
  who, when.
- **Dossier view** — on-demand rendering of an entity's current factual state: trusted facts,
  provisional facts (caveated), open conflicts, confidence and staleness — a structured fact table.
  Read-only in v1 (`dossier show <instance>`). The centerpiece surface.

### Pipeline implication
A **fact-extraction step** after `compress_research` (within/around `persist_research`): an LLM
structured-output pass decomposes a run's citation-bearing findings into **(instance, property,
qualifiers, value, unit, source)** facts. Preserves the core invariant — *the graph owns the agentic
loop*. **Named risks (honest):** extraction's *semantic* fidelity (right qualifiers, right unit, right
as-of) is the top risk and is *not* reduced by the triple schema; structured output remains brittle on
the Gemini/Codex backends (which coerce JSON envelopes, per CLAUDE.md), though a fixed schema is at
least easier to *coerce* than open-ended prose. Both deferred to Architecture.

### Rendering contract
A data-model invariant cannot by itself guarantee a presentation invariant. The renderer has explicit
obligations: **never silently flatten a conflict, never drop a confidence/admission tier, never present
a provisional or contested fact as established.** This governs all *fact-derived* rendering — the
dossier view and the synthesis written into a run report *at run time* — through one canonical render
path. (A stored run report is thereafter immutable, displayed as-written; not re-rendered.)

## 6. Scope and non-goals

**In scope (v1):**
- The **entity-type / instance / property** schema and the **fact base + run report + dossier view**
  data model with provenance, confidence, conflicts, history (P2–P6) — facts are **qualifier-aware**
  and **provenance-keyed** (multiple source-asserted values may coexist per qualified key — that is
  the conflict — while the dossier view presents one *resolved current* value per key).
- The **fact-extraction** pipeline step.
- **Hybrid source space** — scholarly sources (authoritative facts) *and* the open web (currency),
  distinguished by source-trust tiers (P4). Adds a scholarly-source integration over today's search.
- **Confidence-gated promotion** (ingestion unconditional; *promotion* gated) with the defined rule
  and **degraded synthesis** (P7).
- **Conflict surfacing** (genuine value discrepancies under matching qualifiers) inline in output.
- A **read-only dossier view** (`dossier show <instance>`) — the fact-table centerpiece.
- Per-deployment **domain profile** (entity types, properties, qualifiers, trusted-source priors).

**Out of scope / explicit non-goals (v1):**
- **Intrinsically multi-valued, time-series, and relationship facts** — deferred; v1 handles
  properties whose answer is a single value per qualified key (cross-source disagreement on that value
  is the conflict). A property whose *correct answer is itself a set* (a country's official languages)
  or a curve (population by year) or a relation is out of scope. Named so they are not mis-modeled as
  conflicts.
- **Mechanism / causal / qualitative synthesis** — the reframe's deliberate trade (§1). Not a research-
  argument tool.
- **No interactive workflow UI** — no adjudication/resolve/override/triage/fact-editing surface; data
  is captured (P5/P6) and shown read-only; acting on it interactively is deferred.
- No multi-user / collaboration / shared fact bases — single trusted researcher per deployment.
- No backend re-architecture beyond what fact extraction and the scholarly-source connector require
  (flagged: structured-output coercion + a new connector are real, bounded work).

## 7. Competitive reality (why this, not the incumbents)

The honest wedge: **an automatically-built, provenance-and-conflict-aware fact base over entities —
like a personal, auto-researched Wikidata that keeps, and *shows*, the cross-source disagreement,
confidence, and history that structured knowledge bases throw away — aimed at users for whom that
disagreement matters.**

- **Wikidata / Wikipedia** — broad structured facts, but manually curated, one "consensus" value per
  property (disagreement buried in talk/revision history), not built on demand for *your* entities.
- **Knowledge graphs / Diffbot-style extractors** — extract at scale but hosted, opaque on per-value
  provenance, and don't preserve per-fact conflict or confidence history.
- **ChatGPT/Claude (+ search/memory)** — state a fact fast, but ungrounded, no per-value provenance,
  no conflict model, no local ownership, no auditable accumulation. *For low-stakes lookup they win on
  speed* — which is why the persona (§2) must be a user for whom that's not good enough.
- **Manual spreadsheets / Zotero** — what fact-gatherers use today: total control, zero automation, no
  conflict/confidence tracking. This automates the gathering while keeping the rigor.

**Stated honestly (round-5 challenge):** the differentiator is a *feature layer* (visible provenance +
conflict + confidence/history), not a moat; "personal fact base" is historically a category people
avoid building. The bet is that a **high-stakes** user (regulatory, due-diligence, scientific
reference) will value auditable, conflict-aware accumulation enough to switch. The read-only dossier
view is in v1 to make that bet *testable* in session one; if it doesn't land with the chosen persona,
the vision is weaker than it looks. We name this rather than hide it.

## 8. Required-coverage considerations

- **Epistemic safety:** primary risk is presenting unverified/disputed facts as settled. Defenses: P7
  gating + degraded-synthesis caveats, P5 conflict surfacing, P4 confidence honesty, the rendering
  contract. New residual from the reframe: **false conflicts** from misaligned qualifiers, and
  **definitional disagreement** (two "accuracy" numbers true under different metrics) — both can
  mislead; mitigations (qualifier alignment, definition capture) deferred to Architecture.
- **Inclusion / bias:** (type, property)-scoped trust priors encode "whose sources count" — explicit,
  inspectable, revisable, confined to the profile. Cascading invalidation on a trust change needs
  defined recompute behavior — deferred.
- **Legal & compliance:** **append-only history vs. right-to-erasure** reconciled via
  **redaction/tombstoning** (remove protected content, preserve history's shape; propagate to rendered
  artifacts/backups). Licensing, fair-use, derivative-work risk, sensitive-category data, access
  control — named obligations, deferred.
- **Risk & exploitation:** confidence-gaming not defended by corroboration (excluded from promotion)
  or auditability (a post-mortem). Independence detection and laundered-corroboration resistance are
  open. Deferred adjudication must later consider prompt-injection / accidental resolution.
- **Erosion over time:** silently auto-resolving conflicts (violates P5) and dropping history
  (violates P6) are named to be resisted; unbounded growth of conflicts/history is real — GC,
  staleness/temporal-validity, and the "flywheel" are deferred.
- **Economic viability:** extraction + conflict checks add LLM passes; subscription/CLI backends make
  this incremental **but not free** (quotas, latency, provider-policy limits; O(N) conflict-check
  scaling). And (§7) the reframe needs a higher-stakes user to justify the cost — a real constraint.
- **Unknown unknowns:** semantic drift, near-duplicate explosion, operational recovery after
  extractor/prompt upgrades — captured in the deferred doc.

## 9. Open questions (deferred to Architecture)

See `2026-06-12-deferred-living-dossier-platform.md`. Headlines: **entity-instance resolution +
property/qualifier alignment** (relocated successor to "claim identity"); **value-equality with
tolerance + unit normalization** (the new conflict-detection primitive); **multi-valued / time-series
/ relationship facts** (deferred fact kinds); **definitional disagreement** (same property name,
different definition); **property definition** (predefined vs. discovered vs. hybrid); fact-extraction
*semantic* fidelity / non-determinism; provenance binding; **scholarly-source integration** (connectors;
parsing value+unit+qualifiers); independence/echo detection; the promotion "flywheel"; degraded-
synthesis caveat generation; canonical fact-table rendering rules; temporal-validity/staleness;
redaction-vs-append-only; structured-output brittleness; O(N) conflict-check scaling and
recompute-on-trust-change; **beachhead persona** (high-stakes lens); success-metric refinement.

---

*Status: **fact-base reframe converged at v8** (round-6 re-convergence: 4 ADVANCE / 1 minor, the
objection fixed — provenance is part of fact identity). The vision survived going back through the
full review cycle. Next step: resume the **Feature Spec** layer — anchor the concrete high-stakes
persona, scope v1, write user stories + acceptance criteria, and resolve the carried tension that
high-stakes users may need to act on conflicts while v1 is read-only.*
