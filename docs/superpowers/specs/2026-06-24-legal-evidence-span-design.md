# Design: condensed-quote evidence-span match for legal/statute sources

**Status:** built (TDD). Implements Follow-up 3 of
`2026-06-20-dossier-completion-followups.md`.

## Problem

In whole-profile dossiers, search retrieves the right legal sources (e.g. Estonia's
consolidated statutes on `riigiteataja.ee`) but extraction lands **0 valid
`data_protection_law` facts** from the dense legislative prose. The smoke run on
2026-06-22 reproduced this live: the Estonia dossier reported *"Privacy — stated
absence: no data-protection law"* despite the property being researched, and the run
DB held **0 `data_protection_law` facts** across every run.

## Root cause (investigated + reproduced)

Per-source extraction validates each candidate fact in `factbase/extractor.py::extract`:
1. property must exist in the profile,
2. `evidence_span` must be verifiable in the source (`_span_present`),
3. `value` must pass `PropertyDef.validate`.

- **Value validation is not the cause.** `data_protection_law` is `kind: boolean`, and
  boolean validation falls through to `return bool(v)` — any non-empty value passes.
- **`_span_present` is the cause.** It accepts an exact substring, else slides an
  *equal-length* window across the source and accepts on a `SequenceMatcher` ratio ≥ 0.9.
  The extraction prompt invites *"relevant prose verbatim or lightly condensed."* For long,
  dense statute passages the model emits a span that **lightly condenses** the source —
  dropping mid-quote clauses/markers like `(1)` or *"of the European Parliament and of the
  Council."* That span's content is spread across a source region **longer** than the span
  itself, so no equal-length window aligns and the ratio path fails. The grounded fact is
  dropped.

Reproduction (deterministic, no live model): a strictly-verbatim statute span is KEPT; a
lightly-condensed near-verbatim span of the same passage is DROPPED.

## Fix

Add a **condensed long-quote fallback** to `_span_present`, tried only after exact + window
matching fail: token-level in-order coverage of the span against the source, via
`SequenceMatcher` over token lists (an order-preserving LCS, so common leading tokens can't
be greedily mis-anchored).

```
coverage = matched_tokens_in_order / span_tokens   (spans of >= 8 tokens only)
accept if coverage >= 0.80
```

### Threshold rationale (measured on statute fixtures)

| span | token coverage | decision |
|---|--:|---|
| condensed real quote (2 clauses dropped) | 1.00 | KEEP |
| one-synonym-reworded condensed quote | 0.88 | KEEP |
| hallucinated legal-sounding span | 0.29 | drop |
| stopword-salad | 0.47 | drop |
| scattered in-source tokens (not a contiguous quote) | 0.50 | drop |

KEEP cases ≥ 0.88, DROP cases ≤ 0.50 — `0.80` sits in the gap with margin both ways, and
the result is stable when 20× unrelated paragraphs are appended to the source (LCS is
order-preserving, so it does not admit scattered tokens). The ≥ 8-token gate keeps short
spans on the strict exact/window path.

## Hallucination safety

The fallback only *adds* acceptances; it never relaxes the exact/window paths. A fabricated
span is not an in-order subsequence of the source, so it scores far below 0.80 (≤ 0.50 on
the fixtures above). Value↔span binding is unchanged (and was never enforced here).

## Tests

`tests/test_factbase_extractor.py`:
- `_span_present` accepts a condensed long statute quote (not an exact substring),
- `_span_present` rejects a hallucinated legal span and a scattered-token subsequence,
- end-to-end `extract()` keeps a `data_protection_law` fact from a condensed statute span.

## Out of scope

- Trimming the `country_digital_identity` profile (separate task; the ~28k-char extraction
  prompt is a cost/perf concern, not the extraction-drop cause).
- Tying the extracted `value` to the evidence span (pre-existing; unchanged).
