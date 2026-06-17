# Design: Enum Fidelity — set-valued and open-world enums

**Date:** 2026-06-17
**Status:** Design (approved sections, pending written-spec review)
**Branch:** `spec/model-routing-as-data` (design only; implementation is its own branch/plan)

## Problem

Some facts lose fidelity when forced into a single-select enum. The clearest case is
`biometric_capture` (`country_digital_identity.yaml`), whose `value_enum` includes a `multi`
member. A country that captures fingerprint **and** iris **and** photo collapses to `multi`,
which hides *which* modalities were captured — the useful part.

Two distinct lossy patterns appear across the existing profiles:

1. **Set-valued** — the true value is a *subset* of the allowed values, not one of them.
   - `biometric_capture` → `multi` (unhandled).
   - `cbdc_ledger_architecture`, `cbdc_bearer_model`, `cbdc_intermediary_model`,
     `cbdc_privacy_model` (`country_cbdc.yaml:99–170`) invent a `hybrid`/`tiered` member and
     bolt on `qualifier_enums` to explain what the hybrid combines — a workaround for the same
     missing capability.
2. **Open-world** — the enum cannot be exhaustive, so today these punt to `kind: name`
   (e.g. `cbdc_name`, `cbdc_cross_border_role`), discarding enum normalization entirely even
   when most values are known.

Scope chosen for this design: **set-valued + open-world**. (A broader ordinal/graded-collapse
rethink is explicitly out of scope.)

## Approach

Keep `kind: enum` and add two **orthogonal, composable** boolean modifiers rather than new
kinds. This avoids a kind explosion (single/multi × closed/open would otherwise need four
kinds) and slots into the code paths that already special-case `enum`.

```yaml
- name: biometric_capture
  kind: enum
  multi: true                                   # value is a SUBSET of value_enum
  value_enum: [photo, fingerprint, iris, face]  # 'multi' and 'none' members removed
```

```yaml
- name: cbdc_cross_border_role
  kind: enum
  open: true                                    # known vocabulary; outside values kept verbatim
  value_enum: [sender, receiver, hub, observer]
```

- **`multi: true`** → the fact value is a *set* drawn from `value_enum`.
- **`open: true`** → `value_enum` is the *known* vocabulary, not exhaustive; values outside it
  are kept as captured literals rather than dropped or forced into a catch-all.
- The two compose: `multi: true, open: true` = a set that may include unknown members.

Rejected alternatives: **(A)** discrete `enum_set`/`open_enum` kinds — kind explosion, needs a
third `open_enum_set`. **(C)** decompose sets into boolean sub-properties/qualifiers — explodes
property count, destroys the single-fact grouping, miserable for prompting and the matrix; the
awkward CBDC `hybrid`+qualifier workaround is the argument against it.

## Section 1 — Data model

Two optional booleans on a property, valid **only** when `kind: enum`.

**Validation rules** (`profile_schema.py`):
- `multi`/`open` on a non-`enum` kind → raise (mirrors the existing `value_enum` guard at
  `profile_schema.py:40`).
- both require `value_enum` to be present.
- both included in the **semantic hash** (`profile_schema.py:116`) so toggling them is
  structural drift → triggers rebuild, consistent with how `value_enum` changes behave.

**"none" semantics:** for a `multi` enum, the empty set *is* "none captured". Drop explicit
`none`/`multi` members from `biometric_capture`; an empty/absent value means none. (Avoids the
ambiguity of `none` being both a member and the empty set.)

**Workaround retirement:** `biometric_capture` is the reference migration. The CBDC
`hybrid`/`tiered` members are latent sets too, but they're entangled with `qualifier_enums`;
converting them is an **optional follow-up**, not part of this design's core.

## Section 2 — Value representation, storage & canonicalization

The fact value stays a single `TEXT` column (`schema.py:49`) — **no DB migration**.

**Storage form — sorted, comma-joined canonical members:** a `multi` value is stored as the
sorted, deduplicated member list joined by `, ` → `"fingerprint, iris, photo"`. Sorting makes
it order-independent so `{iris, photo}` and `{photo, iris}` are the same fact. Open members
outside `value_enum` are preserved verbatim within that sorted list.

**`canonical_value` (`identity.py:59`) gains a set branch:** for a `multi` enum, split on
commas, normalize each member (map known ones to their `value_enum` casing; keep unknowns when
`open`), sort, re-join → the canonical grouping key. For a non-`multi` `open` enum, behavior is
unchanged — `identity.py:64` already refuses to collapse out-of-enum literals.

**`validate` (`profile.py:52`) gains set/open logic:**
- `multi` + closed: split; every member must be in `value_enum`; **reject the whole fact** if
  any member is unknown (junk signals a bad extraction).
- `multi` + open: split; known members pass, unknown members are kept verbatim; a non-empty
  literal is still required.
- single + open: passes whether or not the value is in `value_enum`.
- single + closed: unchanged.

## Section 3 — Extraction prompting

**Per-property catalog line** (`prompting.py:24–30`) gains a flag-derived hint:
- `multi` closed: `… (enum, select all that apply) | allowed values: [...]`
- `multi` + open: `… (enum, select all that apply; list others verbatim if outside this set) | known values: [...]`
- single + open: `… (enum, use a listed value or give the literal if none fit) | known values: [...]`
- single + closed: unchanged.

"allowed values" becomes "**known values**" when `open` so the model doesn't treat the list as
exhaustive.

**Global rule (`prompting.py:51`)** changes from the absolute "value MUST be one of the listed
allowed values" to a form the per-property hints refine (comma-separated when "select all that
apply"; literals permitted when open).

**Output shape unchanged:** the model still returns a single string per fact (e.g.
`"fingerprint, iris"`); the set is comma-joined text the canonicalizer splits. The
extraction/parse path is untouched.

## Section 4 — Migration of existing data & profiles

**Profile YAML (reference migration):** `biometric_capture` drops `none`/`multi` from
`value_enum`, adds `multi: true`, leaving `value_enum: [photo, fingerprint, iris, face]`. The
semantic-hash change is detected as drift.

**Verified gap (drift/rebuild does NOT cover stale values):**
- `drift.py` is read-only — it only compares profile hashes and returns a `drifted` boolean.
- `rebuild.py::rebuild_structural` recomputes `tuple_key`, `canonical_value`, conflicts, and
  promotion/demotion for retained rows. Its only removal path is for properties that no longer
  exist (`KeyError` → `on_removed`, lines 44–48). It reconstructs `Fact` objects directly from
  rows (line 60) and **never calls `pd.validate`**. A stored `biometric_capture="multi"` row
  would survive rebuild untouched and could still surface as the promoted answer.

**This design closes the gap:** add a **validation sweep** to `rebuild_structural`. While
reconstructing each retained fact, call `pd.validate(value)`; if it now fails, **soft-delete**
the row (reusing the existing `soft_deleted_at` mechanism) and count it under a new
`invalidated` stat. The next research pass re-extracts the real set from sources.

- General, not biometric-specific: correctly handles any future enum tightening.
- Reuses the `validate()` extended in Section 2.
- This is a genuine extension of `rebuild.py`'s contract (it gains a validation
  responsibility) → its own plan task and test.

Existing lossy `"multi"`/`"none"` rows are irreducibly lossy — they cannot be auto-expanded
(that would invent data); soft-delete-and-re-research is the correct resolution. `open` flips
are backward-compatible (previously-rejected literals now validate; stored in-enum values
unaffected) — no data action needed.

## Section 5 — Testing strategy

TDD, mirroring the factbase test layout (pure units first, DB-level last).

**Schema validation (`profile_schema.py`):** `multi`/`open` on non-enum → raises;
`multi`/`open` without `value_enum` → raises; valid flags build and surface on `PropertyDef`;
semantic hash changes on flag toggle, unchanged on description/comment churn.

**Value validation (`profile.py::validate`):** `multi` closed accepts `"fingerprint, iris"`,
rejects `"fingerprint, asdf"`, treats empty as none; `multi` open keeps an unknown member;
single open accepts out-of-enum literal; single closed unchanged (regression).

**Canonicalization (`identity.py::canonical_value`):** `"iris, photo"` == `"photo, iris"`
(order independence); known-member casing/spelling normalizes; unknown open members kept
verbatim (lowercased).

**Prompting (`prompting.py`):** `multi` line renders "select all that apply"; `open` line says
"known values"; single-closed line unchanged.

**Rebuild sweep (`rebuild.py`):** DB-level — seed a fact valid under the old profile, load a
profile where it's now invalid (`biometric_capture="multi"` with `multi` removed); rebuild
soft-deletes it and reports `invalidated: 1`; a still-valid sibling survives.

**Reference-migration regression:** load the migrated `country_digital_identity.yaml`; assert
`biometric_capture` is `multi`, closed, members `[photo, fingerprint, iris, face]`, with no
`multi`/`none` members.

## Touch points summary

| File | Change |
|------|--------|
| `profile_schema.py` | `multi`/`open` fields + validation + semantic-hash inclusion |
| `profile.py` | `PropertyDef` fields; `validate()` set/open logic |
| `identity.py` | `canonical_value` set branch (sorted, order-independent) |
| `prompting.py` | per-property hints + global-rule wording |
| `rebuild.py` | validation sweep → soft-delete + `invalidated` stat |
| `country_digital_identity.yaml` | reference migration of `biometric_capture` |

## Out of scope

- Ordinal/graded-collapse fidelity (a separate, broader rethink).
- Converting the CBDC `hybrid`/`tiered` properties (optional follow-up).
- Any DB schema/column migration (the design deliberately reuses the existing `TEXT` value).
