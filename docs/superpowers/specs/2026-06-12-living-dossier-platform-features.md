# Living Fact Base — Feature Specification (v1)

**Date:** 2026-06-12
**Layer:** Feature Spec (spec-driven development)
**Status:** Draft v2 — incorporated round-1 FS review (5 reviewers, all ANOTHER ROUND). Pending round-2.
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
Profile-defined properties for the `country` entity type. **Qualifiers in *bold* participate in the
conflict key** (vision P5) — two facts conflict only if instance + property + *all* bold qualifiers
match and the values differ.

| Property | Value kind | Conflict-key qualifiers |
|---|---|---|
| `foundational_id_scheme` | name/text | **as-of** |
| `scheme_status` | enum: announced / piloting / operational / mandatory | **as-of** |
| `id_coverage_pct` | percentage | **as-of**, **population_basis** (enum: adults_15plus / total_pop / births / registered_holders), **measured_modeled** (enum: measured / modeled) |
| `biometric_capture` | enum: none / photo / fingerprint / iris / multi | **as-of** |
| `data_protection_law` | boolean + year | **as-of**, **jurisdiction** |
| `legal_basis` | name + year (the *primary enabling instrument*) | **as-of**, **jurisdiction** |

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
- **Confidence-gated promotion** + **degraded synthesis** (vision P7), with the trust bar = registry
  tier ≥ a per-property threshold.
- **Conflict capture & surfacing** — differing values under identical full qualifiers (vision P5).
  v1 uses **exact-value match** (after trivial format canonicalization, e.g. "68,200,000" = "68.2M"
  only where unit is identical); **unit normalization and numeric tolerance are deferred** (vision §9).
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
- **AC2.1** Each property shows its **resolved current value** per the rule: prefer `trusted`; among
  non-conflicting facts with the same qualifiers, the newest `as-of` wins; if trusted facts conflict,
  show **"in conflict"**; if only provisional, show the provisional value **marked provisional**;
  else `unknown`.
- **AC2.2** Provisional values are marked with a concrete token (e.g. `~prov`), never shown as
  established (rendering contract, vision §5).
- **AC2.3** Open conflicts render inline with both values, sources, and as-of
  (`⚠ 99% [WB ID4D, 2024, adults_15plus] vs 87% [GSMA, 2024, adults_15plus]`).
- **AC2.4** Each fact shows its as-of date and a link/handle to its evidence record (US-5).

**US-3 — See cross-source conflict (qualifier-correct).**
- **AC3.1** Two facts on the **same (country, property, and all conflict-key qualifiers)** with values
  unequal under exact-match are linked as an `open` conflict (vision P5).
- **AC3.2** Facts differing in **any** conflict-key qualifier (as-of, population_basis, jurisdiction,
  measured_modeled) are **distinct facts, not a conflict** — e.g. ID4D `adults_15plus` vs GSMA
  `registered_holders` coverage do **not** conflict; they coexist as separate rows.
- **AC3.3** A property with an open conflict among its same-qualifier facts is **not** auto-promoted
  to trusted (vision P7).

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
- **AC5.2** A fact whose source is in the curated registry at/above the property's trust threshold AND
  has no open conflict is promoted to `trusted` (vision P7). A later same-qualifier conflicting fact
  demotes it and opens a conflict.

**US-6 — Refresh and see what changed.**
- **AC6.1** Re-running a country produces a **change-log**: facts added, values changed (with the new
  as-of superseding the old in history, vision P6), properties that became `unknown`/conflicted, and
  what was unchanged. A newer `as-of` value supersedes within the same source+qualifiers; an
  `as-of: unknown` value never supersedes a dated one.

**US-7 — Export for a briefing.**
- **AC7.1** `dossier show`/`compare --format csv|md` emits a file where **every row carries its value,
  source, as-of, and qualifiers** — directly usable in Maya's briefing without re-checking.
- **AC7.2** Provisional and conflicted cells are clearly labelled in the export (not silently dropped).

**US-8 — Run the cohort.**
- **AC8.1** Maya can launch a batched run across the cohort (or a named subset) and see per-country
  progress and failures, without issuing 16 manual commands.

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
- **Implausible value** (coverage 412%) → property-level sanity check rejects; logged, not stored.
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
  `(country, property, qualifiers, value, unit, source, evidence)`; the actual hard NLP.
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

---

*Next step: round-2 multi-agent review of this Feature Spec, then advance to the Architecture layer.*
