# Profile-Driven Completeness & Narrative — Design

**Date:** 2026-06-18
**Status:** Approved (design); pending implementation plan
**Builds on:** the facts-first pipeline (profile selection, catalog-steered retrieval,
per-source extraction, the gap loop) hardened in this session.

## Problem

The profile YAML already steers several stages (profile selection, target-property
resolution, catalog-steered retrieval, per-source extraction, a gap loop). But three gaps
keep it from guaranteeing a complete, narrated dossier:

1. **Scope is question-narrowed.** Facts-first targets only the properties a question needs,
   not everything the profile models.
2. **Narrative is incidental.** `FactRecord.narrative` is an optional per-fact note — never
   steered by the profile, never required, never gap-checked. There is no subject-level
   narrative.
3. **Completeness is value-only.** The gap check asks whether a property has a value, not
   whether its required qualifiers or narrative are present, and there is no notion of a
   property being *confirmed absent* — so "gather everything" has no terminating definition.

This work makes the **profile YAML the single definition of a complete dossier** and drives
the end-to-end process to satisfy it: every property resolved (value + required qualifiers +
required narrative) **or** confirmed-absent, plus a profile-defined subject narrative.

## Goals (confirmed)

1. **Whole-profile completeness** — gather every property the profile models, not just the
   question-scoped subset.
2. **Profile-defined narrative at both levels** — per-property context notes AND a
   subject-level synthesis, with requirements expressed in the profile YAML.
3. **Resolved-or-confirmed-absent** completeness — loop each property to a value
   (+qualifiers +narrative) or an affirmative "no data after searching", bounded by a hard
   budget.

## Architecture (Approach A: profile-as-checklist extending the existing loop)

Generalize the facts-first loop from the question-scoped subset to the whole profile, adding
a per-property **status ledger**, an **absence record**, and a **subject-narrative** node.
Reuses the steering + extraction + gap loop already hardened.

```
write_research_brief  (catalog-steered; round 1 = whole profile, gap rounds = unresolved only)
        |
supervisor -> researchers -> extract_facts        (existing, hardened)
        |
assess_completeness  (NEW): compute the ledger; build a targeted gap brief
        |
   any REQUIRED property incomplete AND budget remains? -- yes --> loop (gap brief)
        | no
        v
synthesize_narrative (NEW) -> answer / persist
```

## Section 1 — Profile YAML extensions (back-compatible; absent fields = today's behavior)

**Per-property `narrative` sub-spec:**
```yaml
- name: id_coverage_pct
  kind: percentage
  description: "Share of the population holding the foundational ID."
  narrative:
    required: true
    guidance: "Explain the population basis and date, and note inclusion gaps
               (rural, women, undocumented) or data-quality caveats."
```

**Per-property completeness tier:**
```yaml
  completeness: required        # required (default) | optional
  absence_allowed: true         # may terminate as confirmed-absent (default true)
```
`required` properties must reach *resolved* or *confirmed-absent*; `optional` are
best-effort. `absence_allowed: false` forbids the absent terminal state (the property must
resolve).

**Profile-level subject-narrative spec:**
```yaml
narrative:
  overview_sections:
    - "How the foundational scheme works and its legal basis"
    - "Coverage and inclusion gaps"
    - "Governance, privacy, and key risks"
```

**Flow of each field (reusing existing machinery):**
- `narrative.guidance` -> appended to its property in `compile_property_catalog`, steering
  **both** the research brief and the per-fact extraction `narrative`.
- `completeness` / `absence_allowed` -> drive the ledger's per-property status and the loop
  termination.
- `narrative.overview_sections` -> the prompt contract for `synthesize_narrative`.

## Section 2 — Per-property status ledger & control flow

`assess_property_status(facts, absences, profile)` is a pure function returning a status per
property:

| Status | Meaning |
|---|---|
| `resolved` | value at the trust bar + all `required_qualifiers` + (if `narrative.required`) a narrative |
| `missing_value` | no value yet |
| `missing_qualifier` | value present, a required qualifier absent |
| `missing_narrative` | value+qualifiers present, `narrative.required`, no narrative |
| `confirmed_absent` | research affirmatively reported "no data after targeted search" |

**Resolved trust bar:** the best available value counts (trusted preferred, a corroborated
provisional acceptable — requiring trusted everywhere would never terminate). The coverage
summary records each property's trust tier so quality stays visible.

**Complete** = `resolved` or `confirmed_absent` (for `absence_allowed: false`, only
`resolved`). The loop continues while any **required** property is incomplete and budget
remains.

**Targeted gap brief:** lists each unresolved property and exactly what is missing —
`<value | qualifier:X | narrative>` — and instructs: "find it, OR if truly unavailable after
searching, state so explicitly." The gap set shrinks every round (only unresolved properties
carry forward), bounding cost.

**Budget:** a new `max_profile_rounds` (default higher than `max_fact_rounds`; the
question-scoped path keeps its existing cap) is the hard backstop. On exhaustion, remaining
incompletes are surfaced in an explicit coverage summary — never silently dropped.

**Confirmed-absent safety:** a property is marked absent ONLY on an affirmative absence
signal from the round's research, never from silence.

## Section 3 — Absence data model & narrative generation

**`property_status` table** — `(instance_key, property_name, qualifiers, status, evidence,
run_id, as_of)`. The gap loop records `confirmed_absent` here with the evidence/search used to
conclude it. The ledger each round = **facts** (resolved / missing) **+** `property_status`
(confirmed-absent). Facts stay purely about values; status/absence is meta.

**Per-property narrative** reuses `FactRecord.narrative`, now steered by `narrative.guidance`
and gap-checked (`missing_narrative`).

**`synthesize_narrative` node** (runs once after the ledger is complete):
- **Input:** resolved facts + their narratives + confirmed-absent list + `overview_sections`.
- **Output:** a structured prose dossier, one section per `overview_section`, grounded ONLY
  in gathered facts — cites sources, states absences explicitly ("No published data on X").
- Best-effort LLM on the cheap chain; **deterministic fallback** to the current
  `_facts_answer_text` listing on failure. This is the whole-profile answer; the question-
  scoped short answer path is unchanged.

## Section 4 — Testing

- **Schema:** new fields parse + validate; a profile with none of them behaves exactly as
  today (back-compat).
- **Catalog compilation:** `narrative.guidance` appears in the compiled catalog.
- **Ledger:** pure-function tests, one per status, plus the resolved-trust-tier rule and
  `absence_allowed: false`.
- **Termination:** loop ends when all required resolved-or-absent or `max_profile_rounds`
  hit; gap set shrinks; confirmed-absent not re-chased.
- **`property_status`:** read/write round-trip; absence recorded only on an affirmative
  signal (mocked).
- **Synthesis:** covers every `overview_section`, grounded (no invention), states absences;
  deterministic fallback on mocked LLM failure.
- **Live behavior** (research quality, absence judgment) verified with an empirical probe
  run, not unit tests.

## Files touched (anticipated)

- `factbase/profile_schema.py`, `factbase/profile.py` — new YAML fields + validation.
- `factbase/prompting.py` — `compile_property_catalog` emits `narrative.guidance`.
- `factbase/schema.py` + migrations — `property_status` table.
- `factbase/<new> completeness.py` — `assess_property_status` ledger (pure).
- `deep_researcher.py` — `assess_completeness` + `synthesize_narrative` nodes, whole-profile
  brief, graph wiring; `max_profile_rounds`.
- `configuration.py` — `max_profile_rounds`, a whole-profile-mode flag.
- Profiles: `country_digital_identity.yaml` gains the new fields as the first adopter.

## Non-goals

- No change to the question-scoped facts-first short-answer path (it stays for narrow Qs).
- No change to extraction's per-source design or the routing/failover work.
- General cross-entity-type narrative templates beyond `overview_sections` are future work.

## Risks / open questions

- **Absence over-claiming:** the model could declare "absent" too readily. Mitigated by
  requiring an affirmative signal + recording its evidence; tune the assessment prompt during
  implementation, and treat low-evidence absences as still-missing under budget.
- **Cost:** whole-profile mode runs more rounds. Mitigated by the shrinking gap set and
  `max_profile_rounds`; measure on the first live probe.
- **Trust-bar tuning:** "corroborated provisional" threshold (how many sources) needs an
  empirical default; start at >=2 sources and adjust.
