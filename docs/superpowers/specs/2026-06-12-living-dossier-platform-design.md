# Living Dossier Platform — Vision & Principles

**Date:** 2026-06-12
**Layer:** Vision + Principles (spec-driven development)
**Status:** Draft v3 — incorporated round-2 feedback; pending round-3 convergence check
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

---

## 1. Vision

Open Deep Research becomes a platform for **building a living dossier**: an accumulating,
provenance-bearing body of knowledge on the subjects a researcher cares about. The product is the
accumulating knowledge, held as three linked things: **immutable per-run reports** (source-grounded
prose), an **accumulating claim base** (the machine-auditable layer), and an on-demand **dossier
view** that renders the current state of a subject.

Each session deepens a body of knowledge the researcher returns to. We are honest that the payoff
is *conditional* (§4, §8): accumulation without quality control produces volume, not value — so the
platform's job is as much about *gating, surfacing disagreement, and aging knowledge* as about
gathering it.

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

> Build and maintain a living dossier per subject — run reports, an accumulating claim base, and a
> current dossier view, all linked.

Success is measured by the quality and groundedness of the accumulated dossier, not by any single
report. **Operationalized (v1 targets, to refine in Feature Spec):** share of trusted claims backed
by a profile-trusted source; conflicts surfaced rather than silently dropped; staleness flagged
rather than served as current. These are the outcomes that distinguish a *living dossier* from a
growing *search history*.

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

### P2 — Knowledge lives as run reports, a claim base, and a dossier view (linked)
A research run produces an **immutable run report** (source-grounded prose, never rewritten) *and* a
set of **claims** extracted from it. Claims **accumulate** across runs into the claim base. A
**dossier view** renders the *current* state of a subject from the claim base on demand.

These are linked, not ranked: run reports are the citable human record of *what a run found*; the
claim base is the machine-auditable layer enabling provenance, confidence, conflict, and history;
the dossier view is the current synthesis. We explicitly **do not** claim "the claims are the truth"
nor that claims can *regenerate* a run report — extraction is lossy, so a claims-derived rendering is
a *view*, not a reconstruction of the original prose. Claims are recorded, provenanced **assertions**.

### P3 — Every claim carries its provenance
Each claim records which **source(s)** it derives from, **when**, and **which run**. (Reality check:
today's pipeline only scrapes URLs from prose; binding a claim to the *specific* evidence for it is
real work, deferred to Architecture. The principle stands; the mechanism is not assumed solved.)

### P4 — Source trust and claim confidence are distinct axes — and both are heuristic
- **Source trust** is a domain-profile prior, **not uniform per source** (a source can be
  authoritative for one claim type, unreliable for another). It is a prior, not a verdict.
- **Claim confidence** is a *computed, heuristic* property from supporting-source trust, known
  contradictions, and (as a *weak, non-authoritative* signal only) corroboration count.

**Honest limitation:** corroboration count **cannot currently detect independence** (syndication,
churnalism, shared upstream studies). It is therefore *displayed* but is **not** an input to trust
*promotion* (P7) until independence detection exists (§8). We do not claim corroboration as an
anti-gaming defense.

### P5 — Disagreement is information; the platform never silently picks a winner
When new research contradicts an existing claim, the platform **records both and marks the conflict**,
and **surfaces it wherever the subject is rendered** (run reports, dossier view). It never
auto-arbitrates a winner. Crucially, gating does not silence disagreement: a contradicting claim is
*surfaced* even while provisional — "excluded from synthesis" (P7) means **not asserted as
established**, never **made invisible**. Interactive *adjudication* (resolve/override/triage) is
deferred (§6); the read-only dossier view *shows* conflicts in v1.

### P6 — Every claim has a history (data-model commitment)
Each claim is an entity with an **append-only revision history**: what changed, when, the cause (new
run, gate promotion/demotion, contradiction, staleness), and the why. Recorded and machine-queryable
in v1; surfaced read-only in the dossier view. Two hard problems are acknowledged and deferred to
Architecture: **claim identity** (proposition-level entity resolution, so revisions attach to the
same claim rather than spawning duplicates — without it the dossier is a pile of fragments and
corroboration cannot even be computed) and **extraction non-determinism** (re-extraction drift must
be distinguished from genuine belief change).

### P7 — Provisional by default; trusted by a defined rule; degraded synthesis when thin
Every newly extracted claim enters **provisional**. It is promoted to **trusted** by an automatic,
domain-profile-policy rule: **the supporting source(s) meet the profile's trust bar AND there is no
open contradiction.** (Corroboration is *not* a promotion input in v1 — see P4.) Promotion/demotion
is automatic by policy because interactive override is deferred; demotion occurs when a contradiction
opens against a previously-trusted claim.

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

- **Subject** — the canonical thing a dossier is about (exists today, keyed by slug).
- **Run report** — immutable source-grounded prose for a run; never rewritten; linked to its claims.
- **Claim** — an atomic, provenanced assertion in the claim base, *retaining the qualifiers (scope,
  time, conditions) needed to be meaningful* — not context-stripped. Carries source(s), origin run,
  admission status, lifecycle status, and computed confidence.
- **Claim revision** — append-only history entry: what/when/cause/why (P6).
- **Source** — external reference with a domain-profile trust prior (P4).
- **Conflict** — first-class link between contradicting claims, `open`/`resolved` (P5).
- **Resolution** — adjudication record (deferred interactive workflow): chosen claim, rationale,
  who, when.
- **Dossier view** — an on-demand rendering of a subject's *current* state from the claim base:
  trusted claims, provisional claims (caveated), open conflicts, confidence and staleness. Read-only
  in v1 (`dossier show <subject>`).

### Pipeline implication
A **claim-extraction step** after `compress_research` (within/around `persist_research`): an LLM
structured-output pass decomposes a run's citation-bearing findings into claims tagged with
source(s). Preserves the core invariant — *the graph owns the agentic loop*. **Named risks:**
extraction quality/granularity is the single biggest risk; structured output is brittle on the
Gemini/Codex backends (which coerce JSON envelopes, per CLAUDE.md). Both deferred to Architecture.

### Rendering contract
A data-model invariant cannot by itself guarantee a presentation invariant. Therefore the renderer
has explicit obligations: **never silently flatten a conflict, never drop a confidence/admission
tier, never present a provisional or contested claim as established.** v1 routes all rendering
(run reports, dossier view) through one canonical render path so this contract holds in one place.

## 6. Scope and non-goals

**In scope (v1):**
- The **run report + claim base + dossier view** data model with provenance, confidence, conflicts,
  history (P2–P6).
- The **claim-extraction** pipeline step.
- **Confidence-gated ingestion** with the defined promotion rule and **degraded synthesis** (P7).
- **Conflict surfacing** inline in rendered output.
- A **read-only dossier view** (`dossier show <subject>`) — the one surface that makes
  conflict/confidence/history visible and the §7 wedge testable in session one.
- Per-deployment **domain profile**.

**Out of scope / explicit non-goals (v1):**
- **No interactive workflow UI.** No conflict-adjudication / resolve-override / triage-queue surface,
  no history-editing UI. The data is captured (P5/P6) and *shown* read-only; *acting* on it
  interactively is deferred. (This is narrower than v2's blanket "no UI": read-only rendering is now
  in scope; interactive mutation of the knowledge state is not.)
- No multi-user / collaboration / shared dossiers — single trusted researcher per deployment.
- No backend re-architecture beyond what claim extraction requires (flagged: structured-output
  coercion may hide real work).

## 7. Competitive reality (why this, not the incumbents)

The honest wedge: **local, owned, provenance-and-conflict-aware accumulation over the open web, with
the cross-run conflict/confidence/history state that incumbents don't keep — now *visible* via the
read-only dossier view.**

- **Elicit** — best-in-class structured extraction + citations, but centered on academic papers and
  hosted. This targets *open-web* domain research with a *local, owned* per-subject dossier.
- **NotebookLM** — grounded answers over *user-supplied* sources; does not *go find and accumulate*
  a growing dossier across runs, nor preserve cross-run conflict/confidence state.
- **Obsidian / Zotero** — own the "knowledge you return to" habit but are manual; no auto-research,
  extraction, confidence, or conflict.
- **ChatGPT/Claude memory + projects** — low-effort recall, but opaque, ungrounded, no provenance or
  conflict model, no local ownership.

**Stated honestly:** the differentiator is the *visible cross-run conflict/confidence/history* layer.
If that isn't compelling enough for a concrete target persona to switch, the vision is weaker than it
looks — which is exactly why the read-only view is in v1 (to make the wedge testable, not asserted)
and why naming the beachhead persona is a Feature-Spec gate.

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

See `2026-06-12-deferred-living-dossier-platform.md`. Headlines: claim identity / entity resolution;
extraction quality/granularity/non-determinism; provenance binding; independence/echo detection;
the promotion "flywheel" (active re-research to earn trust vs. passive); degraded-synthesis caveat
generation; canonical dossier-view rendering rules; temporal-validity/staleness; redaction-vs-
append-only; structured-output brittleness; O(N) scaling and recompute-on-trust-change; beachhead
persona; success metric refinement.

---

*Next step (per spec-driven methodology): round-3 review — a convergence check that the round-2
collisions are resolved and no new ones were introduced — before advancing to the Feature Spec layer.*
