# Living Dossier Platform — Vision & Principles

**Date:** 2026-06-12
**Layer:** Vision + Principles (spec-driven development)
**Status:** Draft v2 — incorporated round-1 multi-agent feedback; pending round-2 review
**Topic:** How the Open Deep Research platform should work for domain researchers

> **Revision note (v2).** Round-1 review (5 independent reviewers: codex, gemini, and three
> internal red-teamers) converged on several structural problems. Three fork-in-the-road
> decisions were resolved and are now baked in: (A) the dossier's *workflow* surfaces are
> deferred — the data model captures conflicts and history, but acting on them is not promised
> in v1; (B) prose reports and structured claims are **dual-canonical and linked**, neither is
> "the truth" at the other's expense; (C) ingestion is **confidence-gated** — claims enter as
> *provisional* and only join the *trusted* base used for synthesis after clearing a bar.
> Remaining hard problems (claim identity, independence detection, extraction quality) are
> named honestly here and deferred to Architecture (see the deferred-questions doc).

---

## 1. Vision

Open Deep Research becomes a platform for **building a living dossier**: an accumulating,
provenance-bearing body of knowledge on the subjects a researcher cares about. The product is
the accumulating knowledge itself, held in two linked, co-canonical forms — **source-grounded
prose** (what a human reads and cites) and **structured claims** (the machine-auditable layer).

Each session does not just answer a question; it *deepens a body of knowledge the researcher
returns to*. The intended payoff is that, over time and with auditing, a subject's dossier
becomes more complete and better-grounded. We are honest that this payoff is *conditional* (see
§4 and §8): accumulation without quality control produces volume, not value.

## 2. Who it's for

**Domain researchers** — people who need a dossier that holds up to scrutiny: cited, auditable,
and explicit about uncertainty and disagreement.

The engine is **domain-adaptable**, configured per deployment via a **domain profile** — not
merely a vocabulary and trusted-source list, but also the field's evidence expectations
(what counts as a strong source, how recency matters). We acknowledge a limit raised in review:
domains differ in more than vocabulary, so "configuration-only" specialization is aspirational;
the first domain the engine is tuned against will inevitably shape its defaults. The domain
profile is the seam where that bias must be made explicit and revisable.

## 3. The core job

> Build and maintain a living dossier per subject — prose and claims, linked.

Everything else — running searches, fanning out to sub-researchers, rendering a report —
serves this job. Success is measured by the quality and groundedness of the accumulated
dossier, not by any single report.

## 4. Principles

These are the epistemic commitments the platform must honor.

### P1 — Trust through traceability *and* gating, audited over time
Research **auto-merges** into the dossier with no human approval gate — but it enters as
**provisional**, not trusted (P7). Trust comes from two things together: **traceability** (every
claim's origin is recorded, P3) and the **provisional→trusted gate** (P7), which keeps unvetted
material out of the knowledge reports are synthesized from.

We explicitly correct a v1 overstatement: *the cost of being wrong is not automatically "low."*
A wrong claim can mislead if it reaches synthesis. The safeguards are (a) gating, so provisional
claims don't feed reports until they clear a bar, and (b) after-the-fact audit. The platform's
value depends on that audit *actually happening*; where it doesn't, provisional claims simply
remain provisional and visibly unverified rather than silently trusted.

### P2 — Knowledge lives as linked prose **and** claims (dual-canonical)
A research run produces source-grounded **prose** *and* a set of **structured claims** extracted
from it. **Neither is subordinate.** Prose is the human-readable, citable artifact; claims are
the machine-auditable layer that enables provenance, confidence, conflict-tracking, and history.
Each links to the other: a claim points to the prose (and sources) it came from; prose can be
regenerated from claims. We do *not* assert "the claims are the truth" — claims are recorded,
provenanced **assertions**, and prose is the grounded narrative they were drawn from.

### P3 — Every claim carries its provenance
Each claim records which **source(s)** it derives from, **when**, and **which run**. Provenance
is what makes the dossier auditable. (Reality check from review: today's pipeline only scrapes
URLs from prose; binding a claim to the *specific* evidence for it is real work, deferred to
Architecture. The principle stands; the mechanism is not assumed solved.)

### P4 — Source trust and claim confidence are distinct axes — and both are heuristic
- **Source trust** is configured in the domain profile, but is **not uniform per source**: a
  source can be authoritative for one claim type and unreliable for another. Trust is therefore
  recorded at the granularity the profile can support, and treated as a prior, not a verdict.
- **Claim confidence** is a *computed, heuristic* property derived from supporting-source trust,
  corroboration count, and known contradictions.

**Honest limitation:** confidence counts distinct sources but **cannot currently detect
independence** — syndication, churnalism, and shared upstream studies inflate apparent
corroboration. We therefore do **not** claim corroboration as a robust anti-gaming defense; it is
a weak signal. Hardening it (independence/echo detection) is an open Architecture problem (§8).

### P5 — Disagreement is information; the platform never silently picks a winner
When new research contradicts an existing claim, the platform **records both and marks the
conflict**. It does not auto-arbitrate. **Scope note (Fork A):** in v1 this is a *data-model*
commitment — conflicts are captured, linked, and surfaced inline wherever prose is rendered so a
disputed point is not shown as settled. An *interactive adjudication workflow* (resolving,
overriding, triaging a conflict queue) is **deferred** until a fit surface exists (§6); v1 does
not promise it through the CLI.

### P6 — Every claim has a history (data-model commitment)
Each claim is an entity with an **append-only revision history**: what changed, when, the cause
(new run, new corroboration, contradiction, gate promotion), and the why. This is a *storage*
commitment in v1 — the lineage is recorded and machine-queryable. A *browsing/exploration
surface* for that history is deferred with P5. Two hard problems are acknowledged, not waved
away: (a) **claim identity** — deciding that a new run's claim *is the same claim* as an existing
one (so revisions attach rather than spawning orphans) requires proposition-level entity
resolution; (b) **extraction non-determinism** — re-extraction drift must be distinguished from
genuine belief change, or history fills with phantom revisions. Both are deferred to Architecture
(§8).

### P7 — Provisional by default; trusted by earning it (confidence-gated ingestion)
Every newly extracted claim enters as **provisional**. It is promoted to **trusted** only when it
clears a configured bar (e.g. sufficient corroboration from sufficiently trusted sources, no open
contradiction). **Report synthesis draws from trusted claims by default**; provisional claims are
visible but clearly marked and excluded from authoritative output unless explicitly included. This
gate is the primary defense against auto-merge contamination. (Claims may also become **stale** —
superseded by time rather than contradicted; temporal validity is acknowledged here and its
mechanism deferred to Architecture.)

## 5. The model (vision-level)

Conceptual entities the principles imply. Concrete schema and mechanism belong to Architecture.

- **Subject** — the canonical thing a dossier is about (exists today, keyed by slug).
- **Report (prose)** — a source-grounded narrative artifact for a run; co-canonical with claims,
  linked to the claims drawn from it.
- **Claim** — an atomic, provenanced assertion belonging to a subject, *retaining the qualifiers
  (scope, time, conditions) needed to be meaningful* — not context-stripped. Carries source(s),
  origin run, status (`provisional`/`trusted`/`stale`/`superseded`), and computed confidence.
- **Claim revision** — an append-only history entry: what/when/cause/why (P6).
- **Source** — an external reference with a domain-profile trust prior (P4).
- **Conflict** — a first-class link between contradicting claims, `open` or `resolved` (P5).
- **Resolution** — an adjudication record (deferred workflow): chosen claim, rationale, who, when.

### Pipeline implication
A **claim-extraction step** is added after `compress_research`, within/around
`persist_research`: an LLM structured-output pass decomposes the run's citation-bearing findings
into claims tagged with their source(s). This preserves the core invariant — *the graph owns the
agentic loop*. **Named risks (review):** extraction quality/granularity is the single biggest
risk to the whole vision; structured output is brittle on the Gemini/Codex backends (which coerce
JSON envelopes — see CLAUDE.md). Both are Architecture concerns, flagged not hidden.

### Conflict & history on current surfaces (v1)
- **Surfacing:** any rendered report for a subject with open conflicts shows them inline
  (`⚠ Conflicting claims on X: [A] (source, date) vs [B] (source, date)`). We acknowledge a
  data-model invariant cannot *guarantee* a presentation invariant — summaries/exports could drop
  the warning — so the rendering contract (never silently flatten a conflict or a confidence tier)
  is itself a requirement on the renderer.
- **Adjudication & history browsing:** deferred (Fork A) until a fit surface exists.

## 6. Scope and non-goals

**In scope (this vision):**
- The dual-canonical **prose + claim** data model with provenance, confidence, conflicts, history.
- The **claim-extraction** pipeline step.
- **Confidence-gated ingestion** (provisional → trusted).
- **Conflict surfacing** inline in rendered prose.
- Per-deployment **domain profile** (vocabulary, trusted-source priors, evidence expectations).

**Out of scope / explicit non-goals (v1):**
- **No new user interface.** Surfaces stay — LangGraph dev/Studio, CLI, SQLite. **Consequence,
  stated honestly (Fork A):** interactive conflict adjudication and history *exploration* are not
  usable in v1; the data is captured for when a surface is built. We are not pretending a CLI
  directive is an adequate adjudication UX.
- No multi-user / collaboration / shared dossiers — single trusted researcher per deployment.
- No change to the model/search backend architecture beyond what claim extraction requires
  (and we flag that this caveat may hide real work on structured-output coercion).

## 7. Competitive reality (why this, not the incumbents)

Review correctly noted strong overlap with existing tools; the vision must answer "why switch":

- **Elicit** — best-in-class structured extraction + per-cell citations, but centered on academic
  papers and a hosted product. This platform targets *open-web domain research*, accumulates a
  *local, owned* dossier per subject, and bills against *subscription/CLI logins*, not per-token
  API or SaaS seats.
- **NotebookLM** — grounded answers over *user-supplied* sources; it does not *go find and
  accumulate* a growing dossier across runs, nor preserve cross-run conflict/confidence state.
- **Obsidian / Zotero** — own the "knowledge you return to" habit but are manual stores; they do
  not auto-research, extract claims, or compute confidence/conflict.
- **ChatGPT/Claude memory + projects** — low-effort per-subject recall, but opaque, ungrounded,
  no provenance/conflict model, no local ownership.

The honest wedge: **local, owned, provenance-and-conflict-aware accumulation, billed against a
subscription, over the open web.** If that wedge isn't compelling for a target researcher, the
vision is weaker than it looks — this is a stated assumption to test, not a settled fact.

## 8. Required-coverage considerations

- **Epistemic safety:** primary risk is presenting unverified/disputed claims as settled.
  Mitigations: P7 gating (provisional excluded from synthesis), P5 inline conflict surfacing,
  P4 confidence honesty. Residual risk: faithful citation of a *misleading* source, and synthesis
  distortion, are not caught by provenance — flagged for Architecture (sampling/quarantine, and a
  high-stakes-domain gate, to be considered).
- **Inclusion / bias:** domain-profile trust priors encode "whose sources count." Must be
  explicit, inspectable, revisable. **Cascading invalidation** (downgrading a source) must have a
  defined recompute behavior — deferred to Architecture.
- **Legal & compliance:** **append-only history (P6) vs. right-to-erasure (GDPR/CCPA) is a real
  contradiction.** Resolution committed at principle level: lineage is append-only in *structure*,
  but personal/retracted/court-ordered content supports **redaction/tombstoning** that removes the
  protected content while preserving the shape of history; erasure must propagate to rendered
  artifacts and backups. "Bounded excerpts" is not a legal standard — licensing, fair-use purpose,
  derivative-work risk, sensitive-category data, and access control are deferred to Architecture as
  named obligations, not hand-waves.
- **Risk & exploitation:** confidence-gaming is *not* defended by corroboration alone (P4 honesty)
  nor by auditability (a post-mortem, not a defense). Independence/echo detection and resistance to
  laundered/synthetic corroboration are open Architecture problems. Deferred-workflow adjudication
  must later consider prompt-injection and accidental resolution.
- **Erosion over time:** the temptations to silently auto-resolve conflicts (violates P5) and to
  drop history to save space (violates P6) are named so they can be resisted. The opposite
  temptation — unbounded growth of conflicts/history/storage — is real and is why P7 gating and
  deferred GC/temporal-validity policy matter.
- **Economic viability:** claim extraction + contradiction checks add LLM passes per run.
  Subscription/CLI backends make this incremental, **but not free** — quotas, latency,
  provider-policy limits on automated use are real constraints (Architecture: cost/throughput
  model, and O(N) contradiction-check scaling).
- **Unknown unknowns:** surfaced by this review (semantic drift, near-duplicate explosion,
  operational recovery after extractor/prompt upgrades) — captured in the deferred doc.

## 9. Open questions (deferred to Architecture)

See `2026-06-12-deferred-living-dossier-platform.md` for the full, categorized list. Headlines:
claim identity / proposition-level entity resolution; extraction quality, granularity, and
non-determinism; independence/echo detection for confidence; migration of legacy free-text
dossiers without fabricating provenance; temporal-validity / staleness policy; redaction-vs-
append-only reconciliation; structured-output brittleness on Gemini/Codex backends; O(N)
contradiction-check scaling and recompute-on-trust-change.

---

*Next step (per spec-driven methodology): round-2 multi-agent feedback review of this revision
before advancing to the Feature Spec layer.*
