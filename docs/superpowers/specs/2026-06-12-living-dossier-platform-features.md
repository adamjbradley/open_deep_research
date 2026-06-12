# Living Fact Base — Feature Specification (v1)

**Date:** 2026-06-12
**Layer:** Feature Spec (spec-driven development)
**Status:** Draft v4 — **Feature Spec converged** (round-3: 4 ADVANCE / 1 minor, the AC5.2 objection
fixed). Final precision pass: AC5.2 excludes `unspecified` tuples from promotion; abstain-default for
ambiguous qualifiers; `as-of` compared as exact-year in v1; added `coverage_kind`. Ready for Architecture.
**Builds on:** `2026-06-12-living-dossier-platform-design.md` (Vision+Principles v8, converged)
**Beachhead:** Digital Public Infrastructure (DPI) researcher

> **Revision note (v2).** Round-1 review converged on one central fix and several trims, now applied:
> **(1) qualifier-complete conflict key** — a conflict is differing values under *identical full
> qualifiers* (incl. `population_basis`, `measured/modeled`); different qualifiers are distinct facts,
> not conflicts. This kills false conflicts on the flagship World-Bank-vs-GSMA case *and* removes the
> need for unit-normalization in v1 (deferred). **(2) Cohort, not all countries** (~15–20).
> **(3) Revised property set** (cut `linked_services_count`; added `scheme_status`,
> `data_protection_law`; `population_basis`/`measured_modeled` are required qualifiers).
> **(4) Curated source registry** for trust tiers (not real-time URL classification). **(5) Evidence
> record** (quoted passage + retrieval timestamp) so "trusted" means the source actually supports the
> value. **(6) New stories:** export/cite, cohort-run management, evidence inspection, refresh+changelog.
>
> **Revision note (v3).** Round-2 review (2 ADVANCE / 2 minor) sharpened the conflict model:
> **(a) `as-of` is a *version* dimension, not identity** — conflict, resolution, and promotion operate
> per `(country, property, non-temporal-qualifier tuple)`; `as-of` selects the *current version* within
> a tuple (newer supersedes; older → history). **(b) Undetermined required qualifier ⇒ abstain** — if
> `population_basis`/`measured_modeled`/`basis` can't be extracted, the fact is `unspecified` and held
> as its own tuple (never guessed, never compared against specified-basis facts, never auto-promoted).
> **(c) Strict exact-match** — removed the "68.2M = 68,200,000" canonicalization (it contradicted
> deferred normalization); v1 compares the value within an identical unit only. **(d) Tier-scoped
> promotion-blocking** — only a conflict *among trust-bar-meeting sources* blocks promotion; a
> lower-tier source's differing value is surfaced but cannot paralyze a World-Bank-tier fact.
> **(e)** Added `basis: de_jure/de_facto` to `scheme_status` and `stage`/`scope` to
> `data_protection_law`. **(f)** Demoted cohort-run orchestration (US-8) and the rich change-log to v1.1.

---

## 1. Beachhead persona & job

**Maya, a DPI researcher** at a think tank / multilateral / funder. She compiles and maintains
comparative facts about **Digital Public Infrastructure across countries** for briefings, country
profiles, and cross-country comparisons that inform policy and funding. Her sources genuinely disagree
(World Bank ID4D vs. GSMA vs. national government vs. academic estimates), facts go stale, and **every
figure she publishes must carry a source and an as-of date** or it is worthless to her audience.

**Why this persona passes the bar:** cross-source factual disagreement is her daily reality and is
*consequential*; provenance and recency are mandatory; a fast ungrounded LLM answer is actively
dangerous to her. She is not doing low-stakes lookup.

**Core jobs:** (1) build/extend a country's Digital Identity fact profile; (2) compare a property
across countries; (3) trust-but-verify — see each source's figure, where they conflict, how current;
(4) export cited facts into her own briefings.

## 2. v1 scope

**The slice:** the **Digital Identity** pillar (one of three) across a **representative cohort of
~15–20 countries**, with per-country dossiers **and** qualifier-matched cross-country comparison.
Payments and Data Sharing pillars, and the full country set, are deliberate fast-follows.

**Illustrative cohort** (final list in build; spans region, language, and DPI maturity): India,
Estonia, Singapore, Nigeria, Kenya, Brazil, Indonesia, Pakistan, Philippines, Ukraine, Rwanda, Peru,
Bangladesh, Ethiopia, Morocco, Mexico. (Mixes MOSIP adopters, mature schemes, and emerging ones.)

### 2.1 Digital Identity property set (v1)
Profile-defined properties for the `country` entity type. Two qualifier roles (round-2 fix):
- **Identity qualifiers** (listed below) define a fact's *tuple* — `(country, property, identity
  qualifiers)`. Conflict, resolution, and promotion all operate **per tuple**.
- **`as-of` is the *version* dimension**, *not* identity: within one tuple, the newest `as-of` is the
  current version and older values move to history (vision P6). Differing `as-of` is **not** a conflict.

A conflict is therefore two facts in the **same tuple** at the **same `as-of` year** with **differing
values**. (v1 treats `as-of` as exact-year for comparison; finer alignment — "mid-2024" vs "2024" —
is deferred to Architecture, §8.) `coverage_kind` is included because "coverage" splits on
enrolled-vs-issued-vs-active credentials (e.g. Aadhaar enrolled ≫ usable) — without it, same-denominator
figures still false-conflict (domain review).

| Property | Value kind | Identity qualifiers (define the tuple) |
|---|---|---|
| `foundational_id_scheme` | name/text | — |
| `scheme_status` | enum: announced / piloting / operational / mandatory | **basis** (de_jure / de_facto) |
| `id_coverage_pct` | percentage | **population_basis** (adults_15plus / total_pop / births / registered_holders), **coverage_kind** (enrolled / issued / active), **measured_modeled** (measured / modeled) |
| `biometric_capture` | enum: none / photo / fingerprint / iris / multi | — |
| `data_protection_law` | boolean + year | **jurisdiction**, **stage** (enacted / in_force), **scope** (comprehensive / sectoral) |
| `legal_basis` | name + year (the *primary enabling instrument*) | **jurisdiction** |

`scheme_status` gets **basis** because a scheme can be *mandatory de jure* yet *piloting de facto* —
without it those would false-conflict. `data_protection_law` gets **stage/scope** because "has a law"
hides enacted-vs-in-force and comprehensive-vs-sectoral distinctions (domain review).

Properties are **profile-predefined** in v1 (discovery deferred, vision §9). `linked_services_count`
and `platform_standard` are cut from v1 (ill-defined / low-convergence per domain review).

### 2.2 In scope
- `country` entity type, cohort instances, the §2.1 property set.
- **Fact extraction** of `(country, property, qualifiers, value, unit, source, evidence)` from runs.
- **Evidence record per fact:** the quoted source passage, the source document identity (canonical
  URL), and the retrieval timestamp — so a fact's value is *shown* to be supported, not just URL-tagged.
- **Curated source registry:** a small, hand-maintained list of known DPI sources with per-property
  trust tiers (e.g. World Bank ID4D, GSMA, national regulators, named academic datasets). Sources not
  in the registry are treated as lowest-tier ("unvetted"). **No real-time URL trust classification.**
  Registry encodes domain corrections: ID4D coverage is flagged `modeled`; national-operator coverage
  figures are *not* ranked above independent academic estimates.
- **Qualifier abstention:** when a required identity qualifier can't be extracted from the source, the
  fact is marked `unspecified` for that qualifier and held as its **own tuple** — never guessed, never
  compared against specified-basis facts, never auto-promoted. (Resolves "infer vs. abstain": v1
  abstains.)
- **Confidence-gated promotion** + **degraded synthesis** (vision P7), with the trust bar = registry
  tier ≥ a per-property threshold. **A conflict blocks promotion only when it is among trust-bar-meeting
  sources** — a lower-tier source's differing value is *surfaced* but cannot block or demote a
  registry-tier fact (so a stray blog can't paralyze a World-Bank figure).
- **Conflict capture & surfacing** — within a tuple, two trust-bar-meeting facts with comparable `as-of`
  and **differing values** are a conflict (vision P5). v1 uses **strict exact-value match within an
  identical unit** (e.g. `99` vs `87` percent); **all format/magnitude canonicalization, unit
  normalization, and numeric tolerance are deferred** (vision §9) — non-identical strings are treated
  conservatively (Architecture defines the minimal canonicalization rule).
- **Append-only per-fact history** (vision P6) — recorded; *computed* staleness flagging deferred (v1
  displays the as-of date and lets Maya judge).
- **Two read-only surfaces** + export:
  - `dossier show <country>` — the country's DI fact table (value, source, evidence link, as-of,
    qualifiers, confidence, conflicts).
  - `dossier compare <property>` — cross-country table, **comparing only facts with matching
    conflict-key qualifiers** (so it never shows denominator-mismatched coverage side by side; mismatch
    renders as separate columns or "uncomparable", not a fake conflict).
  - `--format csv|md` export on both, each row carrying its citation + as-of (Maya's deliverable).

### 2.3 Out of scope (v1)
- Payments and Data Sharing pillars; the full country set (cohort only).
- Interactive conflict adjudication / resolve / override (vision §6). *Read-only is sufficient for
  Maya — she cites the disagreement; she does not resolve it in-tool.*
- Unit normalization, numeric tolerance, real-time URL trust classification (curated registry instead).
- Intrinsically multi-valued / time-series / relationship properties (vision §6, §9).
- Computed staleness lifecycle; property discovery; multi-user.

## 3. User stories & acceptance criteria

**US-1 — Build a country's DI profile.** *Maya researches a country's digital identity; facts populate.*
- **AC1.1** A run on a (country, Digital-Identity) attempts **every** §2.1 property and returns an
  explicit per-property outcome: `value` (with qualifiers+source+evidence), `unknown` (no usable
  source), or `failed` (run/extraction error). The run is "complete" when every property has an
  outcome — not when one fact is found.
- **AC1.2** Each extracted fact enters `provisional` (vision P7) with its evidence record (§2.2).
- **AC1.3** The run produces an immutable run report linked to its facts (vision P2).
- **AC1.4** No usable source for a property → `unknown`. The system never emits a value without an
  evidence passage supporting it.

**US-2 — Read a country dossier.** *`dossier show <country>` shows current DI facts with provenance.*
- **AC2.1** Resolution is **per tuple** `(country, property, identity-qualifiers)`, and a property may
  display several tuples (e.g. coverage under two `population_basis` values). Within each tuple the rule
  is: among trust-bar-meeting facts, the newest `as-of` is current; if two trust-bar-meeting facts at
  comparable `as-of` differ, show **"in conflict"**; if only provisional, show it **marked
  provisional**; else `unknown`. (Distinct tuples coexist; they are never collapsed into one "value".)
- **AC2.2** Provisional values are marked with a concrete token (e.g. `~prov`), never shown as
  established (rendering contract, vision §5).
- **AC2.3** Open conflicts render inline with both values, sources, and as-of
  (`⚠ 99% [WB ID4D, 2024, adults_15plus] vs 87% [GSMA, 2024, adults_15plus]`).
- **AC2.4** Each fact shows its as-of date and a link/handle to its evidence record (US-5).

**US-3 — See cross-source conflict (qualifier-correct).**
- **AC3.1** Two **trust-bar-meeting** facts in the **same tuple** `(country, property,
  identity-qualifiers)`, at the **same `as-of` year**, with values unequal under strict exact-match,
  are linked as an `open` conflict (vision P5).
- **AC3.2** Facts differing in **any identity qualifier** (population_basis, jurisdiction, basis,
  stage, scope, measured_modeled) are **distinct facts, not a conflict** — e.g. ID4D `adults_15plus`
  vs GSMA `registered_holders` coverage do **not** conflict; they coexist as separate tuples. Facts
  differing only in `as-of` are **versions**, not a conflict (newer current; older → history).
- **AC3.3** A **tuple** with an open conflict (per AC3.1) is **not** auto-promoted; the block is scoped
  to that tuple, never the whole property, and a lower-tier differing value neither blocks nor demotes
  a trust-bar-meeting fact (it is surfaced as lower-tier disagreement).

**US-4 — Compare across the cohort.** *`dossier compare id_coverage_pct` → a cross-country table.*
- **AC4.1** One row per cohort country; cells compare **only same-qualifier facts**; differing
  qualifiers appear as separate, labelled columns (e.g. one column per `population_basis`), never
  merged.
- **AC4.2** Countries with only provisional data are shown and marked; `unknown` where absent.
- **AC4.3** The table footer states coverage: N with a value, N unknown, and which qualifier basis
  each column uses.

**US-5 — Inspect the evidence behind a fact.**
- **AC5.1** From any dossier/compare cell, Maya can view the fact's **evidence record**: quoted source
  passage, source document (canonical URL), retrieval timestamp, and the run that produced it.
- **AC5.2** A fact is promoted to `trusted` (vision P7) when **all three** hold: its source meets the
  tuple's trust threshold; it has **no `unspecified` required identity qualifier** (abstained facts are
  never auto-promoted, §2.2); and there is no open conflict in its tuple. A later **trust-bar-meeting**
  fact in the same tuple at the same `as-of` year with a differing value demotes it and opens a
  conflict; a later fact with a **newer `as-of`** supersedes it as the current version (history, not
  conflict).

**US-6 — Refresh a country.**
- **AC6.1** Re-running a country updates its tuples: a newer-`as-of` value from a source becomes the
  current version and the prior moves to history (append-only, vision P6). A run that finds **no**
  source for a property records "not found this run" and **does not** delete or supersede an existing
  dated fact; an `as-of: unknown` value never supersedes a dated one.
- *(v1.1)* A rich four-bucket change-log (added / changed / became-unknown / unchanged) is deferred;
  v1 shows current state + history, from which changes are derivable.

**US-7 — Export for a briefing.**
- **AC7.1** `dossier show`/`compare --format csv|md` emits a file where **every row carries its value,
  source, as-of, and qualifiers** — directly usable in Maya's briefing without re-checking.
- **AC7.2** Provisional and conflicted cells are clearly labelled in the export (not silently dropped).

**US-8 — Run the cohort.** *(v1.1 — deferred; not a v1 minimum.)*
- Batched cohort orchestration with per-country progress/failure is convenience tooling, not a
  factual-correctness need. v1 ships single-country runs (US-1), which can be scripted externally;
  managed batch runs are the first fast-follow.

## 4. Success metrics (v1)
- **Coverage:** # cohort countries with ≥1 trusted DI fact; # properties resolved per country (↑ across runs).
- **Groundedness:** % of presented facts with a registry-tier source + as-of + evidence passage.
- **Conflict correctness:** # genuine same-qualifier conflicts surfaced; **audited false-conflict rate
  ≈ 0** (the qualifier-complete key is judged by this).
- **Trust dynamic:** trusted-vs-provisional ratio per country, growing across runs.
- **Anti-metric (never optimize):** raw fact count — volume without groundedness is the failure mode.

## 5. Edge cases & error paths
- **No source** → `unknown`; never fabricate (AC1.4).
- **Qualitative/range value** ("near-universal") → captured as a typed qualitative marker, not coerced
  to a number; excluded from numeric compare.
- **Missing as-of** → `as-of: unknown`; cannot supersede a dated fact; flagged lower confidence.
- **Country naming** ("Türkiye"/"Turkey"/"Côte d'Ivoire") → canonical ISO-3166 list + alias map (the
  slug function alone is insufficient — build item, §7).
- **Implausible value** (coverage 412%) → rejected by a **per-property validation schema** (ranges /
  enums / regex defined in the profile); logged, not stored. The schema itself is an Architecture
  deliverable (§8).
- **Source in registry but page changed/dead** → evidence record keeps retrieval timestamp + snapshot
  of the quoted passage; a later differing fetch is a new fact, not a silent overwrite.

## 6. Accessibility & cultural assumptions
- Read-only surfaces are plain-text tables (screen-reader friendly); the ⚠ conflict marker must not
  rely on color alone; CSV/MD exports are accessible artifacts.
- **Source/coverage bias:** an all-/many-countries scope must not privilege Global-North sources. The
  curated registry must include regional/national bodies; the coverage metric (§4) must *expose*
  under-sourced countries rather than hide the gap. National-operator figures are tier-flagged as
  potentially incentivized (domain review).

## 7. Hidden dependencies (explicit build items)
- **Curated source registry** (source → per-property tier; modeled/measured + incentivized flags).
- **Canonical country list (ISO 3166) + alias/instance resolution** beyond `slugify`.
- **Fact-extraction contract** — a new structured-output graph node producing
  `(country, property, identity-qualifiers, as-of, value, unit, source, evidence-span)`; the actual
  hard NLP. **Must define the infer-vs-abstain policy:** an identity qualifier is emitted only when the
  source **explicitly states it (or a direct synonym)**; **when in doubt, `unspecified`** (§2.2) — never
  inferred or guessed. (The authoritative required-qualifier set per property is the §2.1 table.) Must
  bind each value to its exact quoted evidence span (AC1.4). This is the load-bearing dependency of the
  whole qualifier model; calibrating "explicitly states" is the extraction node's key tuning task.
- **Evidence capture** — quoted passage + source identity + retrieval timestamp binding.
- **Run-report storage** linked to facts.

## 8. Open questions → Architecture
- Exact value-equality / format-canonicalization rules per property type; when (post-v1) to add
  numeric tolerance + unit normalization (vision §9).
- As-of qualifier alignment (year vs. "mid-2024 estimate") (vision §9).
- Which registry sources to integrate first and *how* (API vs. scrape vs. manual seed); ID4D/GSMA have
  no clean public APIs.
- `dossier compare` rendering (per-qualifier columns, sort, missing data).
- Promotion "flywheel": does `dossier show` trigger refresh, or only an explicit run? (cost-bounded)
- Cohort finalization; per-property trust thresholds.
- **Per-property validation schema** (ranges/enums/regex) for the sanity check (§5).
- **Resolution precedence** within a tuple beyond the AC2.1 rule (e.g. two trust-bar sources at the
  same `as-of` and value — dedup; tie-breaking when as-of precision differs) — the full lattice.

---

*Status: Feature Spec converged at v4 (3 review rounds). Next step: advance to the **Architecture**
layer — whose first deliverable is the **fact-extraction contract** (output schema + the abstain
policy's "explicitly states" calibration + value→evidence-span binding), then the SQLite schema
(entity/property/fact-tuple/conflict/history/evidence), the curated source registry, the conflict +
promotion engine, and the two read-only CLI surfaces. Carry the deferred-questions doc forward.*
