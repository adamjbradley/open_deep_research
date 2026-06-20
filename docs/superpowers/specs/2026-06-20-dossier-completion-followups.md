# Dossier Completion Follow-ups

**Date:** 2026-06-20
**Status:** Backlog (follow-ups from the whole-profile dossier eval)
**Origin:** Estonia whole-profile run (`dossier batch`, agy preset) after the researcher
premature-completion guard (PR #41). The run gathered a strong fact base but timed out in the
gap loop and persisted **no** subject dossier.

## Observed result (evidence)

- agy Estonia run: **135 sources, 52 facts across 5 of 6 core properties** (foundational_id_scheme,
  scheme_status, id_coverage_pct, biometric_capture, legal_basis), rich narratives + qualifiers
  + dates. Missing: `data_protection_law`.
- Hit the 40-min cap at `furthest = extract_facts`; never reached `synthesize_narrative` /
  `persist_research` → `subjects = 0` (no dossier rolled up). Facts ARE in the `fact` table
  (`dossier show` displays them); the subject-level `current_report` is not.
- The prior claude-preset run died even earlier (empty research) — fixed separately by PR #41.

## Follow-up 1 — Partial-persist on abort/timeout (HIGHEST VALUE)

**Problem:** facts accumulate in the `fact` table during the run, but the subject-level dossier
(`subjects.current_report`) is only committed by `persist_research`, the terminal node. A run that
aborts mid-loop persists **nothing** at the subject level — even with 52 good facts. Persisting a
partial dossier (5/6 props) is strictly better than persisting nothing.

**Fix direction:** checkpoint-persist the subject after each completed gap round (or a
finalize-on-abort path) so a timed-out/aborted run still yields a usable dossier (subject +
gathered facts + best-effort narrative). Confirm the empty-run gate still rejects genuinely-empty
runs (0 facts/0 sources) while allowing partial ones.

## Follow-up 2 — Gap loop churns on an unfillable property without bailing out

**Problem:** `assess_completeness` re-loops `extract → assess → extract` on a property it can never
fill (`data_protection_law`), with an over-broad "re-research the whole profile" topic, burning
`max_profile_rounds` + the entire time budget. Observed: **0 net-new facts across multiple rounds**
while the target property stayed empty.

**Fix direction:** (a) after N rounds with no net-new facts for a target property, mark it
`confirmed_absent`/unfillable and finalize instead of looping; (b) scope each gap round's research
topic to ONLY the missing properties (the topic currently re-researches the whole profile —
inefficient and re-fetches already-covered sources). NB: the *search* itself is correct (explicit
`"Personal Data Protection Act Estonia site:riigiteataja.ee"` queries, 135 sources) — the loop's
exit condition and topic scoping are the issue.

## Follow-up 3 — `data_protection_law` extraction gap (search works, extraction fails)

**Problem:** search is correct and on-target (official State Gazette `riigiteataja.ee` consolidated
statutes), but extraction lands **0 valid `data_protection_law` facts** from the dense legislative
text. The lean extractor's verbatim-evidence + value validation likely drops the candidate facts on
statute prose.

**Fix direction:** tune extraction for statute/legal sources — e.g. relax the evidence-span match
for long legal text, or add a legal-document extraction hint. Validate against the Estonian
Personal Data Protection Act page that the run already fetched.

## Priority

1 (partial-persist) > 2 (gap-loop bail-out + topic scoping) > 3 (legal-source extraction).
1 alone turns "gathered 52 facts, saved nothing" into "gathered 52 facts, saved a 5/6 dossier".
