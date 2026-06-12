# Living Fact Base — Feature Specification (v1)

**Date:** 2026-06-12
**Layer:** Feature Spec (spec-driven development)
**Status:** Draft v1 — pending multi-agent review
**Builds on:** `2026-06-12-living-dossier-platform-design.md` (Vision+Principles v8, converged)
**Beachhead:** Digital Public Infrastructure (DPI) researcher

---

## 1. Beachhead persona & job

**Maya, a DPI researcher** at a think tank / multilateral / funder. She compiles and maintains
comparative facts about **Digital Public Infrastructure across countries** — Digital Identity,
Payments, Data Sharing — for briefings, country profiles, and cross-country comparisons that inform
policy and funding. Her sources genuinely disagree (World Bank ID4D vs. GSMA vs. national government
vs. academic estimates), facts go stale (coverage %, scheme status), and **every figure she publishes
must carry a source and an as-of date** or it is worthless to her audience.

**Why this persona passes the round-6 bar:** cross-source factual disagreement is her daily reality
and is *consequential*; provenance and recency are mandatory; a fast ungrounded LLM answer is
actively dangerous to her. She is not doing low-stakes lookup.

**Her core jobs:**
1. *Build/extend* a country's Digital Identity fact profile from authoritative + open sources.
2. *Compare* a property across countries ("which countries have a foundational digital ID?", "ID
   coverage % across South Asia").
3. *Trust but verify* — see which figure each source gives, where they conflict, and how current each is.

## 2. v1 scope

**The slice:** the **Digital Identity** pillar (one of three), across **all countries**, with both
per-country dossiers **and** cross-country comparison. Payments and Data Sharing are deliberately
out of v1 (fast-follow), so the full pillar/property model is proven on a coherent set first.

### 2.1 The Digital Identity property set (starter — refined in Architecture)
A profile-defined set of properties for the `country` entity type, e.g.:

| Property | Value kind | Key qualifiers |
|---|---|---|
| `foundational_id_scheme` | name/text | as-of date |
| `legal_basis` | name + year | as-of date, jurisdiction |
| `id_coverage_pct` | percentage | as-of date, population basis |
| `unique_lifetime_id` | boolean | as-of date |
| `biometric_capture` | enum (none/photo/fingerprint/iris/multi) | as-of date |
| `platform_standard` | enum/text (e.g. MOSIP, custom, OpenID) | as-of date |
| `mobile_credential` | boolean | as-of date |
| `linked_services_count` | integer | as-of date |

Properties are **profile-predefined** in v1 (discovery deferred, vision §9). Each fact retains its
**as-of date** qualifier (and others where relevant), so figures from different years are distinct
facts, not conflicts (vision P5).

### 2.2 In scope
- `country` entity type with all-country instances; the Digital Identity property set (§2.1).
- **Fact extraction** of `(country, property, qualifiers, value, unit, source)` from research runs.
- **Hybrid sources** with trust tiers: authoritative DI sources (World Bank ID4D, GSMA, national
  regulators/standards bodies) > reputable (academic, established orgs) > open web.
- **Confidence-gated promotion** + **degraded synthesis** (vision P7).
- **Conflict capture & surfacing** (value discrepancies across sources under matching qualifiers).
- **Per-fact history** (vision P6).
- **Two read-only surfaces:**
  - `dossier show <country>` — a country's Digital Identity fact table (value, source, as-of,
    confidence, conflicts, staleness).
  - `dossier compare <property>` — a cross-country table for one property (one row per country:
    resolved value, source, as-of, ⚠ if conflicted).

### 2.3 Out of scope (v1)
- Payments and Data Sharing pillars (fast-follow).
- Interactive conflict adjudication / resolve / override (vision §6). *For Maya, read-only is
  sufficient — she cites the disagreement; she does not need to resolve it in-tool.* (Resolves the
  round-6 tension for this persona.)
- Intrinsically multi-valued / time-series / relationship properties (vision §6, §9).
- Multi-user / collaboration.
- Property discovery (predefined only in v1).

## 3. User stories & acceptance criteria

**US-1 — Build a country's DI profile.**
*As Maya, I ask the platform to research a country's digital identity so its facts populate the
dossier.*
- **AC1.1** A run on "<country> digital identity" produces facts for ≥1 property in §2.1, each with a
  source URL and an as-of date (or explicitly `as-of: unknown`).
- **AC1.2** Each fact is stored with admission status `provisional` on first sight (vision P7).
- **AC1.3** The run also produces an immutable run report (prose) linked to the facts (vision P2).
- **AC1.4** If extraction yields no usable fact for a property, that property reads `unknown`, never a
  guessed value.

**US-2 — Read a country dossier.**
*As Maya, I run `dossier show <country>` and see the current DI facts with provenance.*
- **AC2.1** Each property shows: resolved current value (or `unknown`/`in conflict`), source(s),
  as-of date, confidence tier, admission status.
- **AC2.2** Provisional values are visibly marked as such; never presented as established (rendering
  contract, vision §5).
- **AC2.3** Open conflicts render inline (`⚠ 99% [World Bank, 2024] vs 87% [GSMA, 2023]`).
- **AC2.4** Stale facts (as-of older than a profile threshold) are flagged.

**US-3 — See cross-source conflict.**
*As Maya, when two sources disagree on a value under the same qualifiers, I see both.*
- **AC3.1** Two facts on the same `(country, property, as-of)` with values unequal under
  value-equality (tolerance/unit-normalized) are linked as an `open` conflict (vision P5).
- **AC3.2** Differing as-of dates do **not** create a conflict — they are distinct facts (vision P5).
- **AC3.3** A conflicted property is **not** auto-promoted to trusted (vision P7).

**US-4 — Compare across countries.**
*As Maya, I run `dossier compare id_coverage_pct` and get a cross-country table.*
- **AC4.1** One row per country with data: resolved value, source, as-of, ⚠ if conflicted, `unknown`
  if absent.
- **AC4.2** Countries with only provisional data are marked, not omitted.
- **AC4.3** The table states coverage: how many countries have a value vs. unknown.

**US-5 — Trust grows with corroboration/authority.**
*As Maya, a figure from an authoritative source with no conflict becomes trusted.*
- **AC5.1** A fact whose source meets the profile (type, property) trust bar AND has no open conflict
  is promoted to `trusted` (vision P7).
- **AC5.2** A later authoritative figure that conflicts demotes the previously-trusted fact and opens
  a conflict (vision P7).

**US-6 — Facts age.**
*As Maya, I can tell which facts are current vs. stale.*
- **AC6.1** A newer value under a newer as-of date becomes the current fact; the prior remains in
  history as `superseded`, not deleted (vision P6).
- **AC6.2** Lifecycle status (`current`/`stale`/`superseded`) is distinct from admission status
  (`provisional`/`trusted`) (vision P7).

## 4. Success metrics (v1)
- **Coverage:** # countries with ≥1 trusted DI fact; # properties populated per country (trend up
  across runs).
- **Groundedness:** % of presented facts with a profile-trusted source + as-of date.
- **Conflict surfacing:** # genuine cross-source conflicts surfaced; (qualitative) no *false*
  conflicts from as-of/qualifier mismatch in a sample audit.
- **Trust dynamic:** trusted-vs-provisional ratio per country, growing across runs.
- **Anti-metric (never optimize):** raw fact count — volume without groundedness is the failure mode
  the vision warns against.

## 5. Edge cases & error paths
- **No source found** → property `unknown`; never fabricate (AC1.4).
- **Source gives a range/qualitative value** (e.g. "near-universal") → captured as-is with a typed
  marker; not coerced to a false number (intrinsic multi-value still deferred — flag for Architecture).
- **As-of date missing** → `as-of: unknown`; such facts cannot be compared as a time-series and are
  flagged lower-confidence.
- **Country naming / instance resolution** ("Türkiye"/"Turkey"; "Côte d'Ivoire") → entity-instance
  resolution (vision §9, deferred); v1 uses a canonical country list (ISO 3166) to bound this.
- **Extraction produces a malformed/implausible value** (coverage 412%) → rejected by a value sanity
  check; logged, not stored as a fact.
- **Conflicting units** (coverage as % vs. count) → unit normalization required (vision §9); until
  built, flagged rather than mis-compared.

## 6. Accessibility & cultural assumptions
- Read-only surfaces are CLI/text tables — screen-reader friendly by default; comparison output must
  remain valid plain text (no rendering that relies on color alone for the ⚠ conflict marker).
- **Cultural/coverage bias:** an all-countries scope must not privilege Global-North sources. The
  trust profile and "authoritative" tiers must explicitly include regional/national bodies, and the
  coverage metric (§4) must expose which countries are under-sourced rather than hiding the gap.

## 7. Open questions → Architecture
- Concrete value-equality + unit-normalization rules per property type (vision §9).
- As-of qualifier alignment (year vs. "mid-2024 estimate") (vision §9).
- Which scholarly/authoritative DI connectors to integrate first (ID4D, GSMA APIs, scrape vs. feed).
- `dossier compare` rendering rules (sort, missing data, multi-source cells).
- Promotion "flywheel": does asking about one country trigger refresh, or only on demand?
- The Digital Identity property set is a starter — finalize before build.

---

*Next step: multi-agent review of this Feature Spec, then advance to the Architecture layer.*
