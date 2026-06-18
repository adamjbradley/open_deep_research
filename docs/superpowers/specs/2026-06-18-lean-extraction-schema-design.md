# Lean Extraction Schema — Design

**Date:** 2026-06-18
**Status:** Approved (design); pending implementation plan
**Context:** `extract_facts` reliability/robustness rework. Builds on the factbase extraction
path (`deep_researcher.py::_make_fact_model_call`/`extract_facts`, `factbase/extractor.py`,
the `FactRecord`/`ExtractionResult` schema) and the routing step-override added this session.

## Problem

`extract_facts` runs one structured call per captured source: the model must emit
`ExtractionResult = {facts: [FactRecord]}`, where each `FactRecord` is an 8-field nested
schema including a **free-form `qualifiers: dict`**. This is brittle:

1. **Unreliable on cheap models.** The broad, nested schema (esp. the open-ended
   `qualifiers` object) makes gemini-2.5-flash fail structured-output validation (~2/4 on the
   whole-profile schema), forcing a workaround to gemini-2.5-pro (slower, costlier).
2. **All-or-nothing per source.** Strict `with_structured_output(ExtractionResult)` means one
   malformed record discards the whole source's facts.
3. **Quality risk.** The model both decides AND structures qualifiers into a nested dict; the
   open dict invites hallucinated keys/values.
4. **Volume.** One broad call per source re-sends the full catalog each time.

## Goals (all four confirmed)

1. Reliable on cheap models (flash) — no pro/opus needed.
2. Robustness — no all-or-nothing; keep the records a source got right.
3. Quality/accuracy — better qualifier capture, less noise.
4. Lower call volume/cost — stay at one call per source (no per-property explosion).

## Approach (A): lean schema + lenient parse + deterministic qualifier slotting

Keep **one structured call per source** (volume-neutral) but change what the model emits and
how it's parsed, so the rich `FactRecord` is reconstructed by code, not the LLM.

### Section 1 — Lean model-emitted schema

The model emits a JSON array of lean records:
```python
class LeanFact(BaseModel):
    property: str
    value: str
    evidence_span: str            # still verbatim-checked against the source
    narrative: Optional[str] = None
    qualifiers: list[str] = []     # FLAT list of enum tokens, e.g. ["total_pop", "issued"]
```
The key change: `qualifiers` goes from a **nested dict** to a **flat list of enum tokens**.
The prompt already shows each property's allowed qualifier values (via
`compile_property_catalog`); we ask the model to *list the ones that apply*, not to build a
nested object. `unit`/`as_of` are also dropped from the model's burden (resolved from the
value/evidence deterministically or left optional).

Rationale: a flat string list is trivial structured output vs. an open-ended object; the model
still *decides* the qualifiers (preserving quality), it just expresses them simply.

### Section 2 — Lenient parse + deterministic slotting

**Lenient parse.** Replace strict `with_structured_output(ExtractionResult)` with: get the
model's JSON array and validate each element against `LeanFact` independently, keeping valid
records and skipping malformed ones. One garbled record costs that record, not the source.

**Deterministic qualifier slotting** (pure function):
```python
def slot_qualifiers(property_def, tokens) -> dict:
    # enum values are disjoint across a property's qualifiers, so each token maps to exactly
    # one qualifier slot. Drop tokens not in any of this property's qualifier_enums.
    out = {}
    for q, allowed in property_def.qualifier_enums.items():
        allowed_lc = {a.lower() for a in allowed}
        for t in tokens:
            if t.strip().lower() in allowed_lc:
                out[q] = t.strip().lower()
    return out
```
No fuzzy matching, no hallucinated keys. Tokens the model didn't supply leave qualifiers
unset → the completeness ledger flags `missing_qualifier` → the gap loop chases them. This
also tightens validation: a non-enum token can't sneak in.

### Section 3 — Back-compat integration

Reconstruction happens in `factbase/extractor.py::extract`: it takes lean records, rebuilds
the full `FactRecord` (value + slotted qualifiers + evidence_span + narrative), and applies
the **existing** guards — verbatim-`evidence_span` substring check, `pd.validate(value)`,
out-of-enum qualifier drop. The dict shape `extract()` returns is **unchanged**, so `ingest`,
canonicalization, conflict detection, and all downstream tests are unaffected. Only *how
records are produced* changes.

### Section 4 — Flash revert (gated on a probe)

Once flash reliably handles the lean schema, revert the `gemini` preset's `extract_facts`
step-override from `[gemini-2.5-pro, claude-opus-4-6]` back to
`[gemini-2.5-flash, claude-haiku-4-5]`, reclaiming cost/latency. Gated on the empirical probe
(below); if flash still isn't reliable enough, keep pro — the lean schema is still a
robustness/quality win.

## Testing

- **`slot_qualifiers`** (pure): a token slots to its qualifier; disjoint tokens slot to
  different qualifiers; an unknown token is dropped; a qualifier with no token stays unset.
- **Lenient parse**: a JSON array mixing valid and malformed `LeanFact` records keeps exactly
  the valid ones (the all-or-nothing regression).
- **`extractor.extract`** with a fake `model_call` returning lean records → full `FactRecord`s
  with slotted qualifiers; still drops ungrounded (`evidence_span` not in source) and
  invalid-value records.
- **Back-compat**: `extract()`'s return shape unchanged → existing ingest/dedup/conflict tests
  pass untouched.
- **Empirical probe** (not a unit test): run the whole-profile extraction on flash with the
  lean schema and confirm a reliable pass rate (the same probe flash failed 2/4 before). Flip
  the routing only after this passes.

## Files touched (anticipated)

- `deep_researcher.py` — `LeanFact` schema; `_make_fact_model_call` emits/parses lean records
  leniently; the extraction prompt's output-format instructions.
- `factbase/extractor.py` — reconstruct `FactRecord` from lean records + `slot_qualifiers`;
  keep existing guards.
- `factbase/qualifiers.py` (new, small) or `extractor.py` — `slot_qualifiers` pure function.
- `factbase/prompting.py` — output-format guidance (flat qualifier tokens, not a dict).
- `data/model_routing.json` — revert `extract_facts` to flash (Section 4, gated).
- Tests alongside existing extractor/prompting tests.

## Non-goals

- No change to per-source extraction (provenance/corroboration stays).
- No change to ingest/canonicalization/conflict/the FactRecord stored shape.
- Not switching to per-property or batched-source extraction (rejected: per-property
  multiplies calls; batching loses per-source attribution).

## Risks / open questions

- **Qualifier capture rate.** Deterministic slotting may capture fewer qualifiers than a
  strong model emitting a dict, if the model under-lists tokens. Mitigated by: the model still
  decides tokens (it just lists them), and the `missing_qualifier` gap loop chases any it
  misses. Measure on the probe.
- **Shared enum values across qualifiers.** `slot_qualifiers` assumes a property's qualifier
  enums are disjoint (true for the current profile). If a future profile shares a value across
  two qualifiers, the token is ambiguous — slot to the first matching qualifier and document
  the constraint (or require disjoint enums in profile validation).
- **`unit`/`as_of`.** Dropping them from the model means deterministic/optional handling;
  confirm no current property relies on a model-emitted unit that can't be derived from value.
