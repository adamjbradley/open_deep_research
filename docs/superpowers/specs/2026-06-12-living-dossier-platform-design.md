# Living Dossier Platform — Vision & Principles

**Date:** 2026-06-12
**Layer:** Vision + Principles (spec-driven development)
**Status:** Draft — pending multi-agent feedback review
**Topic:** How the Open Deep Research platform should work for domain researchers

---

## 1. Vision

Open Deep Research becomes a platform for **building a living dossier**: an accumulating,
provenance-bearing body of knowledge on the subjects a researcher cares about. The knowledge
base is the product. Individual research reports are byproducts — views rendered from the
dossier on demand.

Each session does not just answer a question; it *deepens a body of knowledge the researcher
returns to and trusts*. The platform's value compounds over time: the more it is used on a
subject, the more complete, more current, and more scrutable that subject's dossier becomes.

## 2. Who it's for

**Domain researchers** — people who need a dossier that holds up to scrutiny: rigorous,
cited, and auditable.

The **engine is domain-agnostic**, but **each deployment is configured for a single field**.
A deployment carries its own subject vocabulary and its own trusted-source list. The same
machinery serves an identity-and-security research group or a pharmacology lab; only the
configuration differs. Generality lives in the engine; specialization lives in the
configuration.

## 3. The core job

> Build and maintain a living dossier per subject.

Everything else — running searches, fanning out to sub-researchers, rendering a report —
serves this job. Success is measured by the quality of the accumulated dossier, not by any
single report.

## 4. Principles

These are the epistemic commitments the platform must honor. They are not optional features;
they define what the platform *is*.

### P1 — Trust through traceability, not gatekeeping
Research **auto-merges** into the dossier with no approval gate. Trust does not come from a
human approving each addition up front; it comes from **every claim being fully traceable**.
The researcher audits and corrects *after the fact* rather than approving *before*. The cost
of being wrong is low because nothing is hidden and everything can be traced and revised.

### P2 — Knowledge is a set of atomic claims
The dossier is not prose. It is a set of discrete, **atomic claims** — each the smallest
proposition a researcher could independently agree or disagree with on its own evidence.
Human-readable prose (reports, briefs) is *rendered from claims on demand*. The prose is a
view; the claims are the truth.

### P3 — Every claim carries its provenance
Each claim records which **source(s)** produced it, **when**, and **which run**. A claim with
no traceable source is not a claim. Provenance is what makes the dossier auditable and is the
foundation of trust (P1).

### P4 — Source trust and claim confidence are distinct axes
- **Source trust** is *configured per deployment* (tiers such as `authoritative`,
  `reputable`, `unvetted`), assigned via the deployment's trusted-source list.
- **Claim confidence** is a *computed property of the claim*, derived from the trust of its
  supporting source(s), the number of *independent* corroborating sources, and whether
  anything contradicts it.

Keeping them separate means a deployment can recalibrate its whole trust posture without
rewriting individual claims, and a reader can always see *why* a claim is trusted.

### P5 — Disagreement is information; never silently pick a winner
When new research contradicts an existing claim, the platform **holds both claims and flags
the conflict** for the researcher to adjudicate. It never silently overwrites or arbitrates.
Visible, unresolved disagreement is itself a form of knowledge and must travel with the
dossier so it can never be unknowingly cited as settled.

### P6 — Every claim has a history, and the history explains itself
Each claim is an entity with an **append-only revision history**. Every revision records:
- **what** changed (the before/after of the claim),
- **when** it changed,
- the **cause** (a new research run, new corroboration, a contradiction, an adjudication), and
- the **why** (the rationale or evidence that drove the change).

The dossier therefore shows not only *what is currently believed* but *how that belief came to
be* — the full lineage of every claim over time. A researcher can reconstruct the evolution of
the subject's knowledge and answer "why do we believe this, and what did we believe before?"

## 5. The model (vision-level)

These are the conceptual entities the principles imply. Concrete schema and mechanism belong
to the Architecture layer; this section establishes *what must exist*.

- **Subject** — the canonical thing a dossier is about (already exists today, keyed by slug).
- **Claim** — an atomic, provenanced assertion belonging to a subject. Carries source(s),
  origin run, computed confidence, and current status.
- **Claim revision** — an entry in a claim's append-only history: what/when/cause/why (P6).
- **Source** — an external reference, tagged with a deployment-configured trust tier (P4).
- **Conflict** — a first-class link between two (or more) claims judged to contradict, with a
  status of open or resolved (P5).
- **Resolution** — the adjudication record for a conflict: the chosen claim, rationale, who,
  and when. The non-chosen claim is marked superseded-by-adjudication, not deleted, and the
  decision itself carries provenance.

### Pipeline implication
A **claim-extraction step** is added at the end of a research run — after `compress_research`,
within or before `persist_research`. An LLM structured-output pass decomposes the run's
synthesized, citation-bearing findings into atomic claims, each tagged with its source(s).
This preserves the platform's core invariant — *the graph owns the agentic loop* — because
extraction is a graph step producing structured data, not a model executing tools.

### Conflict & adjudication on current surfaces
- **Surfacing:** any report or answer rendered for a subject with open conflicts shows them
  inline (e.g. `⚠ Conflicting claims on X: [A] (source, date) vs [B] (source, date)`), so a
  disputed point can never be silently presented as settled.
- **Adjudication:** performed through the existing query surface as a lightweight directive
  (e.g. *"resolve conflict 12 in favor of B because …"*) that writes a resolution record.

## 6. Scope and non-goals

**In scope (this vision):**
- The claim / provenance / confidence / conflict / history **data model**.
- The **claim-extraction** pipeline step.
- **Conflict surfacing and adjudication** through existing surfaces.
- Per-deployment **source-trust configuration**.

**Out of scope (explicit non-goals for now):**
- **No new user interface.** Current surfaces stay — LangGraph dev/Studio, CLI, and SQLite.
  The richness lives in the data model, not a new front end. A dashboard or review-queue UI is
  explicitly deferred.
- No multi-user / collaboration model — the audience is domain researchers, treated as trusted;
  per-contributor attribution beyond run/source provenance is out of scope.
- No change to the model-backend or search-backend architecture
  (`claude_agent_chat.py` / `utils.py`) beyond what claim extraction requires.

## 7. Required-coverage considerations

Surfaced per the spec-driven required-coverage checklist; to be pressure-tested in the
multi-agent review.

- **Safety & harm:** As a research tool for domain professionals, the principal risk is
  *epistemic* harm — confidently presenting unverified or disputed claims as settled fact. P3
  (provenance), P4 (confidence), and P5 (visible conflict) are the primary mitigations. The
  rendering layer must never strip a claim's confidence or conflict flags when producing prose.
- **Inclusion / representation:** Source-trust tiers are deployment-configured and could encode
  bias (whose sources count as "authoritative"). The trust configuration must be explicit,
  inspectable, and revisable — bias should be visible, not baked in silently.
- **Legal & compliance:** The dossier may accumulate copyrighted source text and potentially
  personal data. Provenance must store *references* and bounded excerpts, not wholesale copies;
  data retention/deletion of subjects and claims should be supported. (Detailed handling →
  Architecture.)
- **Risk & exploitation:** A bad actor could seed low-quality sources to inflate a claim's
  confidence. The independent-corroboration requirement in P4 and the auditability in P1/P6 are
  the defenses; the confidence computation must resist single-source or self-citing inflation.
- **Erosion over time:** The temptation will be to silently auto-resolve conflicts "to reduce
  noise." P5 forbids this. The temptation to drop history "to save space" is forbidden by P6.
  These principles exist precisely to resist convenience-driven erosion.
- **Economic viability:** Claim extraction adds an LLM pass per run; on subscription/CLI
  backends this is incremental, not per-token API cost. Acceptable for the target audience.
- **Unknown unknowns:** To be surfaced by adversarial multi-agent review, not assumed away.

## 8. Open questions (deferred to Architecture)

- Exact claim granularity heuristics and the extraction prompt/schema.
- The confidence-scoring function (inputs, weighting, resistance to gaming).
- Contradiction detection: how the extraction step decides two claims conflict.
- Storage schema for claims, revisions, conflicts, resolutions in SQLite, and migration from
  the current free-text `current_report` / `dossier_versions` model.
- How prose rendering selects, orders, and cites claims (and surfaces conflicts) for a report.
- Whether/how existing accumulated dossiers are back-filled into claims.

---

*Next step (per spec-driven methodology): external multi-agent feedback review via
`*.feedback` files before advancing to the Feature Spec layer.*
