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

---

## Added from round-2 review (v3)

20. **Promotion "flywheel" motor.** Trust promotion (P7) and corroboration depend on *later* runs
    about the same subject. Does the platform actively trigger re-research to earn trust, or wait
    passively for the next user query? Without a motor, claims sit provisional indefinitely.
21. **Degraded-synthesis caveat generation.** How reports built partly from provisional claims (P7)
    generate explicit, machine-verifiable caveats — and how the renderer guarantees those caveats
    survive into every output (the rendering contract, §5).
22. **Canonical dossier-view rendering rules.** Exact rules for the on-demand dossier view: how
    trusted vs. provisional vs. stale claims are ordered/labelled, how conflicts are shown, and how
    a claim that wasn't in any single run's prose appears. One canonical render path.
23. **Status as two axes in schema.** Admission (`provisional`/`trusted`) and lifecycle
    (`current`/`stale`/`superseded`) modeled independently, with defined interactions (e.g. a stale
    trusted claim).
24. **Beachhead persona.** A concrete target researcher must be named before the Feature Spec, to
    test the §7 wedge against a real workflow rather than the abstract "domain researchers."
25. **Success-metric refinement.** Turn §3's outcome targets into measurable, instrumented metrics
    distinguishing a living dossier from an accumulating search history.

---

## Added from round-5 review (v7 — fact-base reframe deepening)

26. **Value-equality with tolerance + unit normalization.** The new conflict-detection primitive:
    decide when two values for the same (instance, property, qualifiers) are "the same" (68.2M =
    68,200,000), "different" (68.2M vs 67.9M), or within tolerance; normalize units (mg/L vs µmol/L;
    years vs hours). Without it, conflict detection both false-positives and false-negatives.
27. **Qualifier alignment.** Decide whether two facts share the *same* qualifiers (is "2023" the same
    as-of basis as "mid-2023 estimate"? is benchmark "ImageNet" the same split?). Mis-alignment
    fabricates conflicts or collapses distinct facts. The harder half of the relocated identity problem.
28. **Multi-valued / time-series / relationship facts.** v1 assumes one value per qualified key.
    Real properties are sometimes sets (a country's official languages), curves (population by year),
    or relations (drug→interacts-with→drug). Model these as first-class without treating them as
    perpetual self-conflict.
29. **Definitional disagreement.** Two sources give different values because they define the property
    differently (accuracy under different metric/split; "unemployment" under different denominators),
    not because they disagree on a fact. Capture/compare definitions, not just property names.
30. **Fact semantic-fidelity extraction.** (Sharpened from #2.) The schema is narrow but extracting
    the *right* qualifiers, unit, as-of date, and evidence binding is the real difficulty — measure
    and design for it; do not assume the triple grammar reduces it.
