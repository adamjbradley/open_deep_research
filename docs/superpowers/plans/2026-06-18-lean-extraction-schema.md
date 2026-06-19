# Lean Extraction Schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `extract_facts` reliable on cheap models, robust (no all-or-nothing), and quality-preserving by having the model emit a LEAN record (qualifiers as a flat enum-token list, not a nested dict), parsed leniently per-record, with qualifiers slotted deterministically and the full `FactRecord` rebuilt in the extractor.

**Architecture:** One structured call per source stays (volume-neutral). The model emits a JSON array of `LeanFact` (FactRecord but with `qualifiers: list[str]`). We parse it leniently (validate each record, keep the good ones), slot the flat qualifier tokens back into the proper `{qualifier: value}` dict deterministically against the profile's `qualifier_enums`, and rebuild the existing `FactRecord` shape so all downstream code is unchanged.

**Tech Stack:** Python 3.11, pydantic v2, pytest. Spec: `docs/superpowers/specs/2026-06-18-lean-extraction-schema-design.md`.

## Global Constraints

- Tests run with `.venv/bin/python -m pytest` (bare `python` is not on PATH).
- Already on branch `harden-routing-failover`; do NOT branch or touch main.
- **Back-compatibility:** `extractor.extract()` must return the SAME dict shape it returns today (a `FactRecord`-shaped dict with `qualifiers` as a `{qualifier: value}` dict), so `ingest`, canonicalization, and conflict detection are unaffected.
- The ONLY change to what the model emits: `qualifiers` becomes a flat `list[str]` of enum tokens instead of a nested dict. All other record fields (`property, instance_name, value, unit, as_of, evidence_span, narrative`) are unchanged.
- Existing guards stay: `evidence_span` must be a verbatim substring of the source; `pd.validate(value)`; only valid enum qualifier tokens survive.
- Best-effort: extraction never raises into the run (returns `[]` on failure), as today.

---

### Task 1: `LeanFact` schema + `slot_qualifiers` (deterministic slotting)

**Files:**
- Create: `src/open_deep_research/factbase/lean_extract.py`
- Test: `tests/test_factbase_lean_extract.py`

**Interfaces:**
- Produces: `LeanFact` (pydantic); `slot_qualifiers(property_def, tokens: list[str]) -> dict[str, str]`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_factbase_lean_extract.py
from open_deep_research.factbase.lean_extract import LeanFact, slot_qualifiers
from open_deep_research.factbase.profile_schema import profile_from_dict

PROF = profile_from_dict({"entity_type": "country", "version": "1", "properties": [
    {"name": "cov", "kind": "percentage",
     "identity_qualifiers": ["population_basis", "coverage_kind", "measured_modeled"],
     "qualifier_enums": {"population_basis": ["adults_15plus", "total_pop"],
                         "coverage_kind": ["enrolled", "issued"],
                         "measured_modeled": ["measured", "modeled"]}},
]})
PD = PROF.property("cov")

def test_slot_assigns_each_token_to_its_qualifier():
    out = slot_qualifiers(PD, ["total_pop", "issued", "measured"])
    assert out == {"population_basis": "total_pop", "coverage_kind": "issued",
                   "measured_modeled": "measured"}

def test_slot_drops_unknown_tokens_and_is_case_insensitive():
    assert slot_qualifiers(PD, ["TOTAL_POP", "nonsense"]) == {"population_basis": "total_pop"}

def test_slot_unset_qualifier_when_no_token():
    assert slot_qualifiers(PD, ["issued"]) == {"coverage_kind": "issued"}

def test_leanfact_qualifiers_is_a_flat_list():
    f = LeanFact(property="cov", instance_name="Estonia", value="99",
                 evidence_span="99% hold", qualifiers=["total_pop"])
    assert f.qualifiers == ["total_pop"]
```

- [ ] **Step 2: Run tests, verify fail**

Run: `.venv/bin/python -m pytest tests/test_factbase_lean_extract.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `lean_extract.py`**

```python
"""Lean per-source extraction: the simplified record the model emits + deterministic
reconstruction of the rich qualifiers dict. Keeping the open-ended qualifiers OUT of the
model's structured output is what lets a cheap model emit it reliably."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class LeanFact(BaseModel):
    """What the model emits per fact: FactRecord with qualifiers as a FLAT list of enum
    tokens (e.g. ["total_pop", "issued"]) instead of a nested {qualifier: value} dict."""

    property: str
    instance_name: str
    value: str
    unit: Optional[str] = None
    as_of: Optional[str] = None
    evidence_span: str
    narrative: Optional[str] = None
    qualifiers: list[str] = Field(default_factory=list)


def slot_qualifiers(property_def, tokens: list[str]) -> dict:
    """Slot a flat list of qualifier enum tokens into {qualifier: value}.

    Enum values are disjoint across a property's qualifiers, so each token maps to exactly
    one slot. Tokens not in any of this property's qualifier_enums are dropped. Matching is
    case-insensitive; the canonical (lowercased) token is stored.
    """
    out: dict = {}
    for q, allowed in (getattr(property_def, "qualifier_enums", {}) or {}).items():
        allowed_lc = {a.lower() for a in allowed}
        for t in tokens or []:
            if t and t.strip().lower() in allowed_lc:
                out[q] = t.strip().lower()
                break
    return out
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/python -m pytest tests/test_factbase_lean_extract.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/lean_extract.py tests/test_factbase_lean_extract.py
git commit -m "feat(factbase): LeanFact schema + deterministic slot_qualifiers"
```

---

### Task 2: `parse_lean_facts` (lenient per-record parser)

**Files:**
- Modify: `src/open_deep_research/factbase/lean_extract.py`
- Test: `tests/test_factbase_lean_extract.py`

**Interfaces:**
- Consumes: `LeanFact` (Task 1).
- Produces: `parse_lean_facts(raw: str) -> list[dict]` — tolerant: returns the valid records' `model_dump()` dicts, skipping malformed ones; `[]` if nothing parses.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_factbase_lean_extract.py (add)
from open_deep_research.factbase.lean_extract import parse_lean_facts

def test_parse_keeps_valid_skips_malformed():
    raw = '''[
      {"property":"cov","instance_name":"Estonia","value":"99","evidence_span":"99% hold","qualifiers":["total_pop"]},
      {"property":"cov","value":"bad - missing required fields"},
      {"property":"scheme","instance_name":"Estonia","value":"eID","evidence_span":"the eID"}
    ]'''
    out = parse_lean_facts(raw)
    assert len(out) == 2                       # the malformed middle record is dropped
    assert out[0]["qualifiers"] == ["total_pop"]
    assert out[1]["property"] == "scheme"

def test_parse_tolerates_prose_and_fences_around_the_array():
    raw = "Here are the facts:\n```json\n[{\"property\":\"p\",\"instance_name\":\"X\",\"value\":\"v\",\"evidence_span\":\"e\"}]\n```\nDone."
    out = parse_lean_facts(raw)
    assert len(out) == 1 and out[0]["value"] == "v"

def test_parse_returns_empty_on_garbage():
    assert parse_lean_facts("no json here") == []
    assert parse_lean_facts("") == []
```

- [ ] **Step 2: Run tests, verify fail**

Run: `.venv/bin/python -m pytest tests/test_factbase_lean_extract.py -k parse -v`
Expected: FAIL — `parse_lean_facts` undefined.

- [ ] **Step 3: Implement `parse_lean_facts`**

Add to `lean_extract.py`:
```python
import json
import re

_ARRAY = re.compile(r"\[.*\]", re.S)


def parse_lean_facts(raw: str) -> list[dict]:
    """Lenient parse of the model's output into valid LeanFact dicts.

    Extracts the first JSON array from the text (tolerating markdown fences / surrounding
    prose), then validates each element against LeanFact INDEPENDENTLY -- keeping the valid
    records and skipping malformed ones (no all-or-nothing). Returns [] if nothing parses.
    """
    if not raw or not raw.strip():
        return []
    text = raw.strip()
    arr = None
    try:
        obj = json.loads(text)
        arr = obj if isinstance(obj, list) else obj.get("facts") if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001
        m = _ARRAY.search(text)
        if m:
            try:
                arr = json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                arr = None
    if not isinstance(arr, list):
        return []
    out: list[dict] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        try:
            out.append(LeanFact.model_validate(item).model_dump())
        except Exception:  # noqa: BLE001 - one bad record never drops the rest
            continue
    return out
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/python -m pytest tests/test_factbase_lean_extract.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/lean_extract.py tests/test_factbase_lean_extract.py
git commit -m "feat(factbase): lenient parse_lean_facts (per-record, no all-or-nothing)"
```

---

### Task 3: Extraction prompt emits the lean output format

**Files:**
- Modify: `src/open_deep_research/factbase/prompting.py` (`build_extraction_prompt`)
- Test: `tests/test_factbase_prompting.py`

**Interfaces:**
- Produces: the extraction prompt instructs a JSON array of lean objects whose `qualifiers` is a flat list of enum tokens. (No `with_structured_output` envelope now coerces the shape — the prompt must specify it.)

- [ ] **Step 1: Write failing test**

```python
# tests/test_factbase_prompting.py (add)
def test_extraction_prompt_requests_flat_qualifier_tokens_and_json_array():
    from open_deep_research.factbase.profile_schema import profile_from_dict
    prof = profile_from_dict({"entity_type": "country", "version": "1", "properties": [
        {"name": "cov", "kind": "percentage", "identity_qualifiers": ["population_basis"],
         "qualifier_enums": {"population_basis": ["total_pop"]}},
    ]})
    p = build_extraction_prompt(prof, ["cov"], "Estonia: 99% of total population.", compiled=True)
    low = p.lower()
    assert "json array" in low
    assert "evidence_span" in p
    assert "qualifiers" in low and "list" in low      # flat list, not an object
    assert "do not" in low and "object" in low        # explicit: not a nested object
```

- [ ] **Step 2: Run test, verify fail**

Run: `.venv/bin/python -m pytest tests/test_factbase_prompting.py -k flat_qualifier_tokens -v`
Expected: FAIL — current prompt doesn't specify the JSON array / flat qualifier format.

- [ ] **Step 3: Add the output-format block to `build_extraction_prompt`**

In `build_extraction_prompt`, replace the trailing rules/SOURCE assembly so the prompt ends with an explicit output contract (keep the existing catalog + "emit a qualifier ONLY if the source states it" guidance). Append, before `"\n\nSOURCE:\n" + src`:
```python
            "\nOutput: return a JSON array (no prose, no markdown fences). Each element is an "
            "object with keys: property, instance_name, value, evidence_span, and optionally "
            "narrative. For qualifiers, include a 'qualifiers' key whose value is a flat LIST "
            "of the applicable qualifier enum tokens from the catalog above (e.g. "
            "[\"total_pop\", \"issued\"]) -- do NOT emit qualifiers as a nested object, and "
            "include only tokens the source explicitly supports. evidence_span MUST be a "
            "verbatim substring of the source. If nothing is stated, return [].\n"
```
(Apply to both the `compiled` and non-compiled branches so both end with this contract.)

- [ ] **Step 4: Run test, verify pass**

Run: `.venv/bin/python -m pytest tests/test_factbase_prompting.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/prompting.py tests/test_factbase_prompting.py
git commit -m "feat(factbase): extraction prompt requests lean JSON array + flat qualifier tokens"
```

---

### Task 4: Rewire the pipeline (model_call lenient invoke + extractor reconstruction)

**Files:**
- Modify: `src/open_deep_research/deep_researcher.py` (`_make_fact_model_call`, ~lines 1448-1488)
- Modify: `src/open_deep_research/factbase/extractor.py` (`extract`)
- Test: `tests/test_factbase_extractor.py`

**Interfaces:**
- Consumes: `parse_lean_facts`, `slot_qualifiers`, `LeanFact` (Tasks 1-2).
- Produces: `extract()` returns the SAME `FactRecord`-shaped dicts as today (qualifiers as a `{qualifier: value}` dict), reconstructed from lean records.

- [ ] **Step 1: Write failing test (extractor end-to-end with a fake model_call)**

```python
# tests/test_factbase_extractor.py
import asyncio
from open_deep_research.factbase import extractor as fbextractor
from open_deep_research.factbase.profile_schema import profile_from_dict

PROF = profile_from_dict({"entity_type": "country", "version": "1", "properties": [
    {"name": "cov", "kind": "percentage", "identity_qualifiers": ["population_basis"],
     "qualifier_enums": {"population_basis": ["total_pop"]}},
]})
SRC = "Estonia: 99% of the total population hold the ID."

def test_extract_reconstructs_factrecord_with_slotted_qualifiers():
    async def model_call(source_text, prof):   # returns LEAN dicts (qualifiers as a list)
        return [{"property": "cov", "instance_name": "Estonia", "value": "99",
                 "evidence_span": "99% of the total population hold the ID",
                 "qualifiers": ["total_pop"]}]
    recs = asyncio.run(fbextractor.extract(SRC, PROF, model_call))
    assert len(recs) == 1
    assert recs[0]["qualifiers"] == {"population_basis": "total_pop"}   # list -> dict (back-compat shape)
    assert recs[0]["value"] == "99"

def test_extract_drops_ungrounded_evidence_span():
    async def model_call(s, p):
        return [{"property": "cov", "instance_name": "Estonia", "value": "50",
                 "evidence_span": "this text is NOT in the source", "qualifiers": []}]
    assert asyncio.run(fbextractor.extract(SRC, PROF, model_call)) == []
```

- [ ] **Step 2: Run test, verify fail**

Run: `.venv/bin/python -m pytest tests/test_factbase_extractor.py -v`
Expected: FAIL — `extract` still does old per-dict qualifier validation; `recs[0]["qualifiers"]` is the raw list, not the slotted dict.

- [ ] **Step 3: Update `extractor.extract`**

Replace the per-record loop so it slots qualifiers and rebuilds the record (keep the existing `evidence_span` + `pd.validate` guards; drop the old per-qualifier enum loop — `slot_qualifiers` subsumes it):
```python
from .lean_extract import slot_qualifiers

async def extract(source_text: str, prof: Profile, model_call) -> list[dict]:
    raw = await model_call(source_text, prof)
    norm_source = _norm(source_text)
    kept: list[dict] = []
    for rec in raw or []:
        try:
            pd = prof.property(rec["property"])
        except KeyError:
            continue
        span = rec.get("evidence_span", "")
        if not span or _norm(span) not in norm_source:
            continue
        if not pd.validate(rec.get("value", "")):
            continue
        out = dict(rec)
        out["qualifiers"] = slot_qualifiers(pd, rec.get("qualifiers") or [])  # list -> dict
        kept.append(out)
    return kept
```

- [ ] **Step 4: Update `_make_fact_model_call` (deep_researcher.py) to plain-invoke + lenient parse**

Replace the `with_structured_output(ExtractionResult)` block so the model is invoked as plain text and parsed leniently:
```python
            extraction_model = configurable.model_for("extract_facts", "researcher")
            model = (
                configurable_model
                .with_retry(stop_after_attempt=configurable.max_structured_output_retries)
                .with_config({
                    "model": extraction_model,
                    "model_chain": configurable.model_chain("researcher", "extract_facts"),
                    "stage": "extract_facts",
                    "max_tokens": configurable.researcher_model_max_tokens,
                    "api_key": get_api_key_for_model(configurable.researcher_model, config),
                    "tags": ["langsmith:nostream"],
                })
            )
            from open_deep_research.factbase.lean_extract import parse_lean_facts
            resp = await model.ainvoke([HumanMessage(content=prompt)])
            return parse_lean_facts(str(getattr(resp, "content", "") or ""))
```
(`FactRecord`/`ExtractionResult` classes may remain in the file unused or be removed; leaving them is fine — do not break other imports. Confirm nothing else imports `ExtractionResult`: `grep -rn ExtractionResult src/ tests/`. If only `_make_fact_model_call` used it, you may delete it; otherwise leave it.)

- [ ] **Step 5: Run tests, verify pass + back-compat**

Run: `.venv/bin/python -m pytest tests/test_factbase_extractor.py tests/test_factbase_e2e_ingest.py tests/test_factbase_ingest.py -v`
Expected: PASS (the e2e/ingest tests confirm the reconstructed dict shape still ingests correctly).

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/deep_researcher.py src/open_deep_research/factbase/extractor.py tests/test_factbase_extractor.py
git commit -m "feat(factbase): lean extraction pipeline (lenient invoke + slotted reconstruction)"
```

---

### Task 5: Empirical flash probe + gated routing revert

**Files:**
- Modify: `src/open_deep_research/data/model_routing.json` (the `gemini` preset `extract_facts` override) — ONLY if the probe passes.

- [ ] **Step 1: Probe flash on the whole-profile lean extraction**

Write `/tmp/lean_probe.py` that loads `country_digital_identity`, builds the lean extraction prompt for ALL properties over a real Estonia source, invokes `gemini:gemini-2.5-flash` plainly, runs `parse_lean_facts`, and prints the valid-record count over 5 trials. Run:
`MODEL_ROUTING_PRESET=gemini ODR_PREFLIGHT=off .venv/bin/python /tmp/lean_probe.py`
Expected: flash returns a non-empty, parseable set of lean records on ~5/5 trials (vs the old strict-schema 2/4). Record the pass rate.

- [ ] **Step 2: If flash is reliable (>=4/5), revert the override to flash-primary**

Edit the `gemini` preset in `model_routing.json`:
```json
"extract_facts": ["gemini:gemini-2.5-flash", "claude-haiku-4-5"],
```
(If flash is NOT reliable, leave `extract_facts` on pro and note it — the lean schema is still a robustness/quality win; record the probe result and stop.)

- [ ] **Step 3: Validate routing + run the routing/extractor suite**

Run: `.venv/bin/python -m pytest tests/test_model_routing_resolve.py tests/test_model_routing_schema.py tests/test_factbase_lean_extract.py tests/test_factbase_extractor.py tests/test_factbase_e2e_ingest.py -p no:warnings -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/open_deep_research/data/model_routing.json
git commit -m "perf(routing): revert extract_facts to flash after lean schema (gated on probe)"
```

---

## Self-Review

**Spec coverage:** §1 lean schema → Task 1 (`LeanFact`) + Task 3 (prompt); §2 lenient parse → Task 2, deterministic slotting → Task 1 (`slot_qualifiers`); §3 back-compat reconstruction → Task 4 (`extractor.extract`, unchanged output shape, ingest tests); §4 flash revert (gated) → Task 5; testing → each task's tests + Task 5 probe. All spec sections mapped.

**Placeholder scan:** No TBDs. Task 4 Step 4's "delete ExtractionResult if unused, else leave it" is a concrete grep-gated instruction, not a placeholder. Task 5 is explicitly gated on a measured probe result.

**Type consistency:** `LeanFact` fields (`property, instance_name, value, unit, as_of, evidence_span, narrative, qualifiers: list[str]`), `slot_qualifiers(property_def, tokens) -> dict`, `parse_lean_facts(raw) -> list[dict]`, and `extract()`'s reconstructed dict (`qualifiers` → dict) are consistent across tasks. The extractor consumes lean dicts (qualifiers list) from `_make_fact_model_call` and emits FactRecord-shaped dicts (qualifiers dict) — the contract pair lands together in Task 4.

**Ordering:** 1 (schema + slotter) → 2 (parser) → 3 (prompt) → 4 (wire model_call + extractor together — the only live-contract change) → 5 (gated revert).
