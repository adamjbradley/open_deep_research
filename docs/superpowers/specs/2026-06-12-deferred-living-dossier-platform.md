# Deferred Questions — Living Dossier Platform

**Date:** 2026-06-12
**Source:** Round-1 multi-agent review of the Vision+Principles draft (codex, gemini, 3 internal
red-teamers). Items here were judged below the Vision layer's abstraction and are carried forward
as primary input to the **Architecture** layer. The deferred doc accumulates across layers.

---

## Technical / data model

1. **Claim identity & semantic equivalence.** Proposition-level entity resolution across runs:
   when is a new claim *the same* claim (→ revision), a *replacement*, or a genuinely *conflicting*
   claim? Paraphrase, negation, numeric tolerance, scope/time qualification, ontology evolution all
   blur this. Without a stable identity key, append-only history (P6) and conflict links cannot
   both stay coherent. **Highest-priority architecture problem.**
2. **Extraction quality, granularity, and non-determinism.** What granularity is "atomic" without
   being trivial or compound? How is extraction validated/measured? Re-extraction drift must be
   distinguished from real belief change so history isn't polluted with phantom revisions.
3. **Provenance binding.** Today only `extract_sources()` (regex over prose) exists. Need
   sentence/claim-level binding of a claim to the *specific* evidence for it, not run-level URL
   scraping — otherwise provenance is fabricated (violates P3).
4. **Independence / echo detection for confidence (P4).** Detect syndication, churnalism, shared
   upstream studies, citation circularity, same-parent-org. Required before corroboration count is
   a trustworthy confidence input or any anti-gaming claim can be made.
5. **Confidence scoring function.** Concrete inputs, weighting, and the provisional→trusted
   promotion bar (P7). Must resist single-source and laundered/synthetic corroboration.
6. **Temporal validity / staleness.** Mechanism for claims that are true-then-false-by-time
   (validity windows, expiry, "stale" status distinct from "contradicted/superseded").
7. **Migration of legacy dossiers.** Existing `subjects.current_report` / `dossier_versions` are
   free-text prose with no original sources. Back-filling claims must not fabricate provenance.
8. **Structured-output brittleness on Gemini/Codex backends.** Per CLAUDE.md these coerce JSON
   tool-selection envelopes; a high-volume, schema-strict extraction pass is where that is most
   fragile. Quantify and design around it.
9. **Scaling & recompute.** Contradiction detection is ~O(N) LLM comparisons per new claim per
   run; conflicts/history grow unbounded. Define GC/retention policy and recompute behavior when a
   source's trust changes (cascading invalidation of downstream confidence and resolved conflicts).
10. **Operational recovery.** Behavior under extractor upgrades, prompt changes, model regressions,
    corrupted migrations, and mass recomputation that rewrites confidence/conflict relationships.

## Legal / compliance

11. **Append-only vs. erasure.** Implement redaction/tombstoning that satisfies GDPR/CCPA and
    source retraction / court orders while preserving lineage *shape*; propagate deletion into
    rendered artifacts and backups.
12. **Copyright / licensing.** Derivative-work risk of LLM-extracted claims; fair-use purpose;
    what "bounded excerpt" actually means legally; sensitive-category data handling.
13. **Access control & audit logging.** Even single-user, the per-claim source+date+run graph is a
    re-identification surface; define protection and audit logging.

## Product / interaction (for when the deferred surface is built)

14. **Conflict adjudication workflow.** The interactive triage/resolve/override experience deferred
    by Fork A — including resistance to prompt-injection and accidental resolution.
15. **History exploration surface.** How a researcher browses a claim's lineage (P6) usefully.
16. **Audit ergonomics.** P1 depends on after-the-fact audit *happening*; design must make
    auditing lower-effort than ignoring it, or the trust model is unfunded.

## Strategic / open

17. **Decision-quality evidence (codex's hardest question).** What measurable evidence would show
    an auto-merged, LLM-extracted dossier yields *safer, more accurate* decisions than retaining
    source-grounded reports — once contamination, staleness, unresolved conflicts, and unaudited
    backlog are counted? Define the metric before betting the platform on the thesis.
18. **Domain-profile depth.** How far "configuration-only" specialization can really go before a
    new domain needs engine changes (evidence hierarchies, temporal semantics, entity resolution).
19. **Adoption / cold-start.** Early-dossier value is low and there's no workflow UI; what makes
    the first 90 days worthwhile enough to keep using it?
