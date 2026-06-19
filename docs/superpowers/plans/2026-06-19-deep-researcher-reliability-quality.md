# deep_researcher reliability, quality & efficiency — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `deep_researcher` never silently produce empty dossiers, extract more of the facts present in sources, cover all profile properties, and stop wasting LLM calls / tripping rate limits.

**Architecture:** Surgical changes to the existing LangGraph pipeline + factbase tail + batch ledger. Five independent units (A reliability gate, B whole-profile mode, C extraction recall, D efficiency, E handoff fixes), each unit-testable offline with fakes.

**Tech Stack:** Python 3.11, LangGraph, aiosqlite, pytest, `uv` for running. Run tests with `uv run pytest`.

## Global Constraints

- Run all tests with `uv run pytest <path> -q -p no:warnings`.
- Match surrounding code style; no new dependencies (`difflib`, `unicodedata` are stdlib).
- Each task: write failing test → confirm fail → minimal implementation → confirm pass → commit.
- Do NOT change the fact/registry SQLite schema except via a new `(version, sql)` tuple appended to `STEPS` in `src/open_deep_research/factbase/schema.py` (next integer after the current max, which is 9 → use 10).
- Offline tests only (no live model/network). Use fakes for model calls.

---

## File map

| File | Change |
|---|---|
| `src/open_deep_research/deep_researcher.py` | A1 supervisor nudge; A2 persist empty-gate; D2 extract_facts semaphore; E1/E2 assess_sufficiency |
| `src/open_deep_research/factbase/extractor.py` | C1 `_norm` unicode; C2 fuzzy span fallback |
| `src/open_deep_research/factbase/prompting.py` | C3 `_SOURCE_CAP` raise |
| `src/open_deep_research/factbase/batch_ledger.py` | A3 `attempt_count` + increment |
| `src/open_deep_research/factbase/schema.py` | A3 migration (v10) |
| `src/open_deep_research/factbase/batch.py` | A3 worker empty-gate; B1/D1 `default_run_one` config |
| `src/open_deep_research/claude_agent_chat.py` | D2 per-attempt down-tracker skip |
| `src/open_deep_research/data/model_routing.json` | C4 extract_facts extractor |
| `.env`, `.env.example` | B1 remove global `FACTS_FIRST_MODE` |
| `tests/test_reliability_gate.py` (new) | A1, A2, A3 |
| `tests/test_factbase_extractor.py` | C1, C2 |
| `tests/test_extraction_prompt.py` (new) | C3 |
| `tests/test_batch_modes.py` (new) | B1, D1 |
| `tests/test_failover_integration.py` | D2 |
| `tests/test_assess_sufficiency.py` (new) | E1, E2 |

---

## Unit A — Reliability gate

### Task A1: Supervisor blank-turn corrective nudge

**Files:**
- Modify: `src/open_deep_research/deep_researcher.py` (`supervisor_tools`, ~line 569)
- Test: `tests/test_reliability_gate.py`

**Interfaces:**
- Produces: behavior — a supervisor turn with `no_tool_calls` and no prior `ConductResearch` loops back to `supervisor` with a corrective `HumanMessage`, instead of exiting to `END`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reliability_gate.py
import asyncio
from langchain_core.messages import AIMessage, HumanMessage
import open_deep_research.deep_researcher as dr


def _cfg():
    return {"configurable": {"max_researcher_iterations": 4, "allow_clarification": False}}


def test_blank_supervisor_turn_is_nudged_not_ended():
    # Supervisor's latest message has NO tool calls and no research has run yet.
    state = {
        "supervisor_messages": [
            HumanMessage(content="Research Brazil digital identity."),
            AIMessage(content="Here is some prose with no tool call."),
        ],
        "research_iterations": 1,
        "research_brief": "Research Brazil digital identity.",
    }
    cmd = asyncio.run(dr.supervisor_tools(state, _cfg()))
    assert cmd.goto == "supervisor"  # looped back, NOT __end__
    msgs = cmd.update["supervisor_messages"]
    assert msgs and "ConductResearch" in msgs[-1].content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_reliability_gate.py::test_blank_supervisor_turn_is_nudged_not_ended -q -p no:warnings`
Expected: FAIL — currently `supervisor_tools` returns `goto="__end__"` for `no_tool_calls`.

- [ ] **Step 3: Write minimal implementation**

In `supervisor_tools`, BEFORE the existing exit block `if exceeded_allowed_iterations or no_tool_calls or research_complete_tool_call:` (~line 569), insert:

```python
    # Guard against a blank turn (model returned text / no tool call) before any research ran.
    # The CLI backends raise on a bad envelope, but an API model (e.g. NVIDIA) can return a
    # text AIMessage with empty tool_calls -> the old no_tool_calls exit ended research empty
    # (the Brazil failure). Nudge it to dispatch ConductResearch and loop, bounded by the cap.
    if no_tool_calls and not conducted_research and not exceeded_allowed_iterations:
        return Command(
            goto="supervisor",
            update={"supervisor_messages": [HumanMessage(content=(
                "You did not call any tool. You MUST call ConductResearch with one or more "
                "specific, standalone research_topic instructions before finishing. "
                "Dispatch the necessary research now."))]},
        )
```

(`conducted_research` is already computed above the existing `ResearchComplete` guard; `HumanMessage` is already imported.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_reliability_gate.py::test_blank_supervisor_turn_is_nudged_not_ended -q -p no:warnings`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_reliability_gate.py src/open_deep_research/deep_researcher.py
git commit -m "fix(graph): nudge supervisor on blank turn instead of ending research empty"
```

### Task A2: persist_research empty-run gate (status=error + counts)

**Files:**
- Modify: `src/open_deep_research/deep_researcher.py` (`persist_research`, ~line 1273-1422)
- Test: `tests/test_reliability_gate.py`

**Interfaces:**
- Produces: `persist_research` returns `{"report_id", "subject", "fact_count", "status"}`. When `fact_count == 0` AND the run captured 0 `raw_text` sources, the run is logged `status="error"` and the dossier is left unchanged (mirrors the existing `_report_is_failed` path).
- Consumes (helper): a new module function `await _run_fact_count(conn, run_id) -> int` and `await _raw_text_source_count(db_path, thread_id) -> int`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reliability_gate.py  (append)
from open_deep_research.deep_researcher import _is_empty_run


def test_is_empty_run_true_when_no_facts_and_no_sources():
    assert _is_empty_run(fact_count=0, raw_text_source_count=0) is True

def test_is_empty_run_false_when_any_facts():
    assert _is_empty_run(fact_count=3, raw_text_source_count=0) is False

def test_is_empty_run_false_when_sources_present():
    # sources gathered but 0 facts = "thin", NOT empty (don't auto-fail legitimately sparse countries)
    assert _is_empty_run(fact_count=0, raw_text_source_count=5) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_reliability_gate.py -k is_empty_run -q -p no:warnings`
Expected: FAIL — `_is_empty_run` not defined.

- [ ] **Step 3: Write minimal implementation**

Add near the top-level helpers in `deep_researcher.py` (e.g. after `_report_is_failed`):

```python
def _is_empty_run(*, fact_count: int, raw_text_source_count: int) -> bool:
    """A run that gathered nothing: 0 researched facts AND 0 raw_text sources captured.

    Distinct from a 'thin' run (sources gathered, few facts) -- that may be a legitimately
    sparse country and is surfaced, not failed.
    """
    return fact_count == 0 and raw_text_source_count == 0
```

Then in `persist_research`, after the report-failed check and before the success return, compute the counts and gate. Concretely: after `extract_facts` has run (it precedes persist in the graph), query counts using the preallocated run id and thread id, and add an empty-run branch that logs `status="error"` (reuse the existing error path block) when `_is_empty_run(...)`. Thread `fact_count` and `status` into every return dict:

```python
    # Empty-run gate: a run that captured no raw_text sources AND extracted no facts is a
    # failed research attempt (the Brazil class), not a real dossier. Log it as an error so the
    # batch ledger retries it on resume -- never merge it into the subject dossier.
    thread_id = (config.get("configurable") or {}).get("thread_id")
    prealloc = state.get("prealloc_run_id")
    fact_count = await _run_fact_count(db_path, prealloc) if prealloc else 0
    src_count = await _raw_text_source_count(db_path, thread_id) if thread_id else 0
    if _is_empty_run(fact_count=fact_count, raw_text_source_count=src_count):
        run["status"] = "error"
        run["error"] = "empty run: 0 facts, 0 raw_text sources"
        subject_for_log = state.get("subject") or topic
        run_id = await log_research_run(db_path, slugify(subject_for_log), run,
                                        run_id=state.get("prealloc_run_id"))
        logger.error("Empty run (0 facts/0 sources); logged as error for retry.")
        return {"report_id": run_id, "subject": subject_for_log,
                "fact_count": 0, "status": "error"}
```

Add the two count helpers (use `aiosqlite`, exclude the static population run by counting only the run's own `run_id`):

```python
async def _run_fact_count(db_path: str, run_id) -> int:
    import aiosqlite
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM fact WHERE run_id=?", (run_id,))
        return int((await cur.fetchone())[0])

async def _raw_text_source_count(db_path: str, thread_id: str) -> int:
    import aiosqlite
    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM run_source WHERE thread_id=? AND capture_status='raw_text'",
            (thread_id,))
        return int((await cur.fetchone())[0])
```

Also add `"fact_count": fact_count, "status": "completed"` to the existing success return dicts so callers always get them.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_reliability_gate.py -k is_empty_run -q -p no:warnings`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_reliability_gate.py src/open_deep_research/deep_researcher.py
git commit -m "feat(graph): persist_research flags empty runs (0 facts/0 sources) as error"
```

### Task A3: Batch ledger attempt_count + worker auto-retry on empty

**Files:**
- Modify: `src/open_deep_research/factbase/schema.py` (append migration v10)
- Modify: `src/open_deep_research/factbase/batch_ledger.py` (`mark` increments attempt_count; `summary` includes it)
- Modify: `src/open_deep_research/factbase/batch.py` (`default_run_one` returns dict; `worker` gates empty→failed)
- Test: `tests/test_reliability_gate.py`

**Interfaces:**
- Consumes: `default_run_one(...) -> dict` with keys `report_id`, `fact_count`, `status` (from Task A2's persist return — `deep_researcher.ainvoke` result carries them).
- Produces: `BatchLedger.mark(..., status="failed")` increments `batch_item.attempt_count`. `worker` marks `failed` (not `done`) when the run is empty/error.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reliability_gate.py  (append)
import aiosqlite
import open_deep_research.factbase.schema as fbschema
import open_deep_research.factbase.migrations as fbmig
from open_deep_research.factbase.batch_ledger import BatchLedger


def test_failed_mark_increments_attempt_count(tmp_path):
    db = str(tmp_path / "t.db")
    async def go():
        async with aiosqlite.connect(db) as conn:
            await fbmig.apply(conn, fbschema.STEPS)
            led = BatchLedger(conn, "b_x", profile_name="p", profile_hash="", list_spec="Brazil")
            await led.ensure_run()
            await led.upsert_item("BRA", "Brazil")
            await led.mark("BRA", status="failed", error="empty")
            await led.mark("BRA", status="failed", error="empty")
            cur = await conn.execute("SELECT attempt_count FROM batch_item WHERE instance_key='BRA'")
            return (await cur.fetchone())[0]
    assert asyncio.run(go()) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_reliability_gate.py::test_failed_mark_increments_attempt_count -q -p no:warnings`
Expected: FAIL — no `attempt_count` column.

- [ ] **Step 3: Write minimal implementation**

Append to `STEPS` in `schema.py` (after the `(9, …)` entry; use the next version number):

```python
    (10, """
    ALTER TABLE batch_item ADD COLUMN attempt_count INTEGER DEFAULT 0;
    """),
```

In `batch_ledger.py` `mark`, increment `attempt_count` only on `failed`:

```python
    async def mark(self, instance_key: str, *, status: str, run_id: str | None = None,
                   error: str | None = None) -> None:
        """Update an item's status (+ optional run_id/error). 'failed' increments attempt_count."""
        _check_status(status)
        inc = ", attempt_count = attempt_count + 1" if status == "failed" else ""
        await self._conn.execute(
            f"UPDATE batch_item SET status=?, run_id=?, error=?, updated_at=datetime('now'){inc} "
            "WHERE batch_id=? AND instance_key=?",
            (status, run_id, error, self.batch_id, instance_key))
        await self._conn.commit()
```

In `batch.py` `default_run_one`, return the richer result:

```python
    return {"report_id": str(result.get("report_id") or ""),
            "fact_count": int(result.get("fact_count") or 0),
            "status": str(result.get("status") or "completed")}
```

In `batch.py` `worker`, gate on emptiness:

```python
            try:
                outcome = await self._run_one(
                    name, key, profile_name=self._profile, db_path=self._db)
                # Back-compat: a bare run id string counts as a non-empty success.
                if isinstance(outcome, dict):
                    rid, status, fc = outcome["report_id"], outcome["status"], outcome["fact_count"]
                else:
                    rid, status, fc = str(outcome), "completed", 1
                if status == "error" or fc == 0:
                    await led.mark(key, status="failed", error="empty run (auto-retry)", run_id=rid)
                else:
                    await led.mark(key, status="done", run_id=rid)
            except Exception as e:  # noqa: BLE001
                await led.mark(key, status="failed", error=str(e))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_reliability_gate.py::test_failed_mark_increments_attempt_count -q -p no:warnings`
Expected: PASS. Also run the existing batch tests: `uv run pytest tests/ -k batch -q -p no:warnings` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/schema.py src/open_deep_research/factbase/batch_ledger.py src/open_deep_research/factbase/batch.py tests/test_reliability_gate.py
git commit -m "feat(batch): auto-retry empty runs on resume + attempt_count"
```

---

## Unit B — Whole-profile mode for batches

### Task B1: default_run_one → whole_profile_mode; drop global FACTS_FIRST_MODE

**Files:**
- Modify: `src/open_deep_research/factbase/batch.py` (`default_run_one` configurable dict)
- Modify: `.env` (remove `FACTS_FIRST_MODE=true`), `.env.example` (document)
- Test: `tests/test_batch_modes.py` (new)

**Interfaces:**
- Produces: a `default_run_one`-shaped config resolves `whole_profile_mode == True`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_batch_modes.py
from open_deep_research.configuration import Configuration

def _default_run_one_configurable(profile="country_digital_identity"):
    # Mirrors batch.default_run_one's dict (keep in sync).
    return {"profile_name": profile, "use_knowledge_base": False, "allow_clarification": False,
            "persist_results": True, "max_concurrent_research_units": 2,
            "max_researcher_iterations": 2, "whole_profile_mode": True,
            "summarize_search_results": False}

def test_batch_config_is_whole_profile(monkeypatch):
    for k in ("FACTS_FIRST_MODE", "WHOLE_PROFILE_MODE", "SUMMARIZE_SEARCH_RESULTS"):
        monkeypatch.delenv(k, raising=False)
    c = Configuration.from_runnable_config({"configurable": _default_run_one_configurable()})
    assert c.whole_profile_mode is True
    assert c.facts_first_mode is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_batch_modes.py::test_batch_config_is_whole_profile -q -p no:warnings`
Expected: FAIL (currently the configurable doesn't set the flag; and if `.env` still sets `FACTS_FIRST_MODE=true`, env precedence would force facts_first — the test deletes that env var, so the actual failure is `whole_profile_mode is False`).

- [ ] **Step 3: Write minimal implementation**

In `batch.py` `default_run_one`, add to the configurable dict:

```python
        "whole_profile_mode": True,         # comprehensive per-profile dossier (targeted gap rounds)
        "summarize_search_results": False,  # extraction reads raw text; summaries are wasted here (Unit D1)
```

In `.env`, delete the line `FACTS_FIRST_MODE=true` (it forces facts-first on every run, overriding per-run mode). In `.env.example`, add a comment:

```
# FACTS_FIRST_MODE / WHOLE_PROFILE_MODE: leave UNSET. Mode is chosen per run (batches use
# whole_profile_mode via default_run_one). Setting these env vars forces the mode globally.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_batch_modes.py::test_batch_config_is_whole_profile -q -p no:warnings`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/batch.py tests/test_batch_modes.py .env.example
git commit -m "feat(batch): run dossiers in whole_profile_mode; stop forcing facts-first globally"
```

---

## Unit C — Extraction recall

### Task C1: Unicode-normalize span verification

**Files:**
- Modify: `src/open_deep_research/factbase/extractor.py` (`_norm`)
- Test: `tests/test_factbase_extractor.py`

**Interfaces:**
- Produces: `_norm` collapses Unicode (NFKD) + non-breaking spaces so a clean evidence span matches source text containing NBSP/curly-quotes/en-dashes.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_extractor.py  (append)
from open_deep_research.factbase.extractor import _norm

def test_norm_unicode_nbsp_and_quotes():
    src = "Coverage is “99%” as of 2023"   # NBSP + curly quotes
    span = 'Coverage is "99%" as of 2023'                 # plain space + straight quotes
    assert _norm(span) in _norm(src)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_factbase_extractor.py::test_norm_unicode_nbsp_and_quotes -q -p no:warnings`
Expected: FAIL — current `_norm` keeps NBSP/curly quotes distinct.

- [ ] **Step 3: Write minimal implementation**

In `extractor.py`:

```python
import unicodedata

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = s.replace(" ", " ").replace("“", '"').replace("”", '"') \
         .replace("‘", "'").replace("’", "'").replace("–", "-").replace("—", "-")
    return _WS.sub(" ", s.strip().lower())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_factbase_extractor.py::test_norm_unicode_nbsp_and_quotes -q -p no:warnings`
Expected: PASS. Also run the full extractor suite: `uv run pytest tests/test_factbase_extractor.py -q -p no:warnings` → PASS (no regressions).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/extractor.py tests/test_factbase_extractor.py
git commit -m "fix(extract): unicode-normalize evidence-span verification (recover dropped facts)"
```

### Task C2: Fuzzy span fallback for near-miss quotes

**Files:**
- Modify: `src/open_deep_research/factbase/extractor.py` (`extract` span gate)
- Test: `tests/test_factbase_extractor.py`

**Interfaces:**
- Produces: `_span_present(span_norm: str, source_norm: str) -> bool` — True if `span_norm` is a substring OR a high-similarity (≥0.9) match against the best window of `source_norm`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_extractor.py  (append)
from open_deep_research.factbase.extractor import _span_present

def test_span_present_accepts_near_paraphrase():
    src = _norm("aadhaar is brazil's foundational identity scheme operated by uidai")
    near = _norm("aadhaar is brazil's foundational identity scheme operated by the uidai")  # tiny diff
    far = _norm("the moon is made of cheese and has no relation to identity systems at all")
    assert _span_present(near, src) is True
    assert _span_present(far, src) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_factbase_extractor.py::test_span_present_accepts_near_paraphrase -q -p no:warnings`
Expected: FAIL — `_span_present` not defined.

- [ ] **Step 3: Write minimal implementation**

In `extractor.py`:

```python
import difflib

_FUZZY_THRESHOLD = 0.9

def _span_present(span_norm: str, source_norm: str) -> bool:
    """Span verification: exact substring, else a high-similarity window match.

    The fuzzy fallback rescues paraphrased/whitespace-mangled quotes that are still
    substantially present, without admitting hallucinated spans (threshold is strict).
    """
    if not span_norm:
        return False
    if span_norm in source_norm:
        return True
    n = len(span_norm)
    if n < 12 or n > len(source_norm):  # too short to fuzz safely / longer than source
        return False
    sm = difflib.SequenceMatcher(None, span_norm, "")
    # Slide a window of the span's length across the source; accept on a strong ratio.
    step = max(1, n // 4)
    for i in range(0, len(source_norm) - n + 1, step):
        window = source_norm[i:i + n]
        if difflib.SequenceMatcher(None, span_norm, window).ratio() >= _FUZZY_THRESHOLD:
            return True
    return False
```

Then change the span gate in `extract` from:

```python
        if not span or _norm(span) not in norm_source:
            continue
```

to:

```python
        if not _span_present(_norm(span), norm_source):
            continue
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_factbase_extractor.py -q -p no:warnings`
Expected: PASS (new test + no regression in existing extractor tests).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/extractor.py tests/test_factbase_extractor.py
git commit -m "feat(extract): fuzzy evidence-span fallback (recover near-miss quotes, strict threshold)"
```

### Task C3: Raise the source-text cap

**Files:**
- Modify: `src/open_deep_research/factbase/prompting.py` (`_SOURCE_CAP`)
- Test: `tests/test_extraction_prompt.py` (new)

**Interfaces:**
- Produces: `build_extraction_prompt` includes source text beyond char 8000 (up to the new cap).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_extraction_prompt.py
from open_deep_research.factbase.prompting import build_extraction_prompt
from open_deep_research.factbase import profile as fbprofile

def test_source_cap_includes_text_past_8000():
    prof = fbprofile.load("country_digital_identity")
    marker = "UNIQUE_FACT_MARKER_12345"
    src = ("x" * 12000) + " " + marker
    prompt = build_extraction_prompt(prof, None, src, compiled=False)
    assert marker in prompt   # text at ~char 12000 must reach the model
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_extraction_prompt.py -q -p no:warnings`
Expected: FAIL — `_SOURCE_CAP=8000` truncates before the marker.

- [ ] **Step 3: Write minimal implementation**

In `prompting.py`:

```python
_SOURCE_CAP = 24000
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_extraction_prompt.py -q -p no:warnings`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/prompting.py tests/test_extraction_prompt.py
git commit -m "fix(extract): raise source-text cap 8000->24000 so long pages aren't truncated"
```

### Task C4: Strong extract_facts model in the nvidia preset

**Files:**
- Modify: `src/open_deep_research/data/model_routing.json` (nvidia preset `step_overrides.extract_facts`)
- Test: `tests/test_model_routing_schema.py`

**Interfaces:**
- Produces: nvidia preset routes `extract_facts` to a strong, non-throttled extractor first.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_model_routing_schema.py  (append)
def test_nvidia_extract_facts_leads_with_strong_extractor():
    from open_deep_research.model_routing import load_routing
    chain = load_routing().presets["nvidia"].step_overrides["extract_facts"]
    assert chain[0] == "agy:gemini-3.1-pro-high"   # strong recall, not the throttled minimax-m3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_model_routing_schema.py::test_nvidia_extract_facts_leads_with_strong_extractor -q -p no:warnings`
Expected: FAIL — current head is `nvidia:minimaxai/minimax-m3`.

- [ ] **Step 3: Write minimal implementation**

In `model_routing.json`, nvidia preset `step_overrides`:

```json
        "extract_facts": ["agy:gemini-3.1-pro-high", "claude-opus-4-6"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_model_routing_schema.py -q -p no:warnings`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/data/model_routing.json tests/test_model_routing_schema.py
git commit -m "perf(routing): nvidia extract_facts -> agy gemini-3.1-pro (recall + avoid throttle)"
```

---

## Unit D — Efficiency & rate-limits

### Task D1: Summarization off in dossier mode

> Implemented together with Task B1 (the `default_run_one` config sets `summarize_search_results=False`). Add this verification test.

**Files:**
- Test: `tests/test_batch_modes.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_batch_modes.py  (append)
def test_batch_disables_summarization(monkeypatch):
    for k in ("SUMMARIZE_SEARCH_RESULTS",):
        monkeypatch.delenv(k, raising=False)
    c = Configuration.from_runnable_config({"configurable": _default_run_one_configurable()})
    assert c.summarize_search_results is False
```

- [ ] **Step 2-4:** Run it; it passes once B1 lands (the configurable sets it). If run before B1, it FAILs (default True) — confirming the wiring.

Run: `uv run pytest tests/test_batch_modes.py::test_batch_disables_summarization -q -p no:warnings`
Expected: PASS (after B1)

- [ ] **Step 5: Commit** (folded into B1's commit if implemented together, else:)

```bash
git add tests/test_batch_modes.py
git commit -m "test(batch): assert dossier mode disables per-source summarization"
```

### Task D2: Bound extract_facts concurrency + per-attempt down-skip

**Files:**
- Modify: `src/open_deep_research/deep_researcher.py` (`extract_facts` gather, ~line 1690)
- Modify: `src/open_deep_research/claude_agent_chat.py` (`configurable_claude_model.ainvoke` failover loop, ~line 1463)
- Test: `tests/test_failover_integration.py`

**Interfaces:**
- Produces: a model marked down mid-`gather` is skipped by concurrent in-flight peers (re-check `tracker.is_down` per attempt). Extraction concurrency is bounded by a semaphore.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_failover_integration.py  (append)
def test_inflight_peer_skips_just_marked_down_model(monkeypatch):
    # Two concurrent calls share a tracker. The first marks the primary backend down
    # (backend_fatal); the second, started after, must skip the primary entirely.
    tracker = new_run_tracker()
    constructed = []
    script = {
        "gemini:gemini-2.5-pro": Exception("429 insufficient_quota"),  # backend_fatal -> mark down
        "claude-opus-4-8": "OK",
    }
    _patch_build(monkeypatch, script, constructed)
    cfg = {"model_chain": ["gemini:gemini-2.5-pro", "claude-opus-4-8"], "stage": "extract_facts"}
    model = configurable_claude_model().with_config(cfg)
    asyncio.run(model.ainvoke("first"))   # marks gemini backend down
    constructed.clear()
    asyncio.run(model.ainvoke("second"))  # must skip gemini
    assert constructed == ["claude-opus-4-8"]
```

(This already passes via `available_chain` at call start; the NEW assertion that exercises the per-attempt skip is the next test.)

```python
def test_per_attempt_skip_within_available(monkeypatch):
    # available_chain is computed once; ensure the loop re-checks is_down before each attempt so a
    # model marked down by a peer between chain-build and this attempt is not tried.
    tracker = new_run_tracker()
    tracker_seen = {}
    constructed = []
    def fake_build(model_string, max_tokens=None):
        constructed.append(model_string)
        # mark the primary down right after the chain was built but before we try it
        if model_string == "nvidia:x":
            tracker.mark_backend_down("nvidia")
        from tests.test_failover_integration import _FakeModel
        return _FakeModel(model_string, {"nvidia:x": Exception("429"), "claude-opus-4-8": "OK"})
    monkeypatch.setattr(cac, "build_chat_model", fake_build)
    model = configurable_claude_model().with_config(
        {"model_chain": ["nvidia:x", "claude-opus-4-8"], "stage": "extract_facts"})
    assert asyncio.run(model.ainvoke("x")) == "OK"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_failover_integration.py -k "inflight or per_attempt" -q -p no:warnings`
Expected: the per-attempt test FAILs (loop tries `nvidia:x` even after it's down).

- [ ] **Step 3: Write minimal implementation**

In `claude_agent_chat.py` `ainvoke`, inside the `for idx, model_string in enumerate(available):` loop, add a re-check at the top:

```python
        for idx, model_string in enumerate(available):
            if tracker.is_down(model_string) and idx < len(available) - 1:
                continue  # a peer marked this down after the chain was built; skip to backup
            try:
                result = await self._materialize(...)
                ...
```

In `deep_researcher.py` `extract_facts`, bound the gather with a semaphore:

```python
            sem = asyncio.Semaphore(int(os.getenv("EXTRACT_FACTS_CONCURRENCY",
                                                  str(configurable.max_concurrent_research_units or 4))))
            async def _extract_one(s):
                async with sem:
                    try:
                        recs = await fbextractor.extract(s["text"], prof, model_call)
                        for r in recs:
                            r.setdefault("source_url", s["source_url"])
                        return recs
                    except Exception as e:
                        logger.warning("Extraction failed for %s: %s", s["source_url"], e)
                        return []
```

(`os` and `asyncio` are already imported in deep_researcher.py.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_failover_integration.py -q -p no:warnings`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/claude_agent_chat.py src/open_deep_research/deep_researcher.py tests/test_failover_integration.py
git commit -m "perf(extract): bound concurrency + skip in-flight-marked-down models (no 429 storms)"
```

---

## Unit E — Handoff correctness

### Task E1: assess_sufficiency narrows target_properties on the gap round

**Files:**
- Modify: `src/open_deep_research/deep_researcher.py` (`assess_sufficiency`, ~line 1772-1776)
- Test: `tests/test_assess_sufficiency.py` (new)

**Interfaces:**
- Produces: the gap-round `Command.update` includes `target_properties == missing`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_assess_sufficiency.py
import asyncio
import open_deep_research.deep_researcher as dr

def test_gap_round_narrows_target_properties(monkeypatch, tmp_path):
    # Force the "missing" branch deterministically by stubbing coverage.
    monkeypatch.setattr(dr, "_target_property_coverage",
                        lambda grouped, targets: ({t: False for t in targets}, {}))
    # Resolve a subject + a fake instance key so the DB branch is entered, then short-circuit the query.
    import open_deep_research.factbase.entities as ent
    monkeypatch.setattr(ent.CountryResolver, "resolve", lambda self, s: "BRA")
    async def fake_grouped(self, key): return []
    import open_deep_research.factbase.query as q
    monkeypatch.setattr(q.FactQuery, "show_grouped", fake_grouped)

    state = {"target_properties": ["legal_basis", "id_coverage_pct"], "subject": "Brazil",
             "fact_rounds_used": 0}
    cmd = asyncio.run(dr.assess_sufficiency(state, {"configurable": {"max_fact_rounds": 3,
                                                                     "database_path": str(tmp_path/'x.db')}}))
    assert cmd.goto == "write_research_brief"
    assert cmd.update["target_properties"] == ["legal_basis", "id_coverage_pct"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_assess_sufficiency.py::test_gap_round_narrows_target_properties -q -p no:warnings`
Expected: FAIL — `target_properties` not in the update.

- [ ] **Step 3: Write minimal implementation**

In `assess_sufficiency`, the gap-round return adds `target_properties`:

```python
        return Command(
            goto="write_research_brief",
            update={"missing_information": gap, "fact_rounds_used": rounds_used + 1,
                    "target_properties": missing},
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_assess_sufficiency.py::test_gap_round_narrows_target_properties -q -p no:warnings`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/deep_researcher.py tests/test_assess_sufficiency.py
git commit -m "fix(graph): facts-first gap round targets only missing properties"
```

### Task E2: assess_sufficiency treats DB error as still-missing

**Files:**
- Modify: `src/open_deep_research/deep_researcher.py` (`assess_sufficiency` except handler, ~line 1765)
- Test: `tests/test_assess_sufficiency.py`

**Interfaces:**
- Produces: a raising fact-base lookup routes to a gap round (within budget), not to `answer_from_facts`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_assess_sufficiency.py  (append)
def test_db_error_loops_instead_of_finishing(monkeypatch, tmp_path):
    import open_deep_research.factbase.entities as ent
    monkeypatch.setattr(ent.CountryResolver, "resolve", lambda self, s: "BRA")
    import open_deep_research.factbase.query as q
    async def boom(self, key): raise RuntimeError("db locked")
    monkeypatch.setattr(q.FactQuery, "show_grouped", boom)
    state = {"target_properties": ["legal_basis"], "subject": "Brazil", "fact_rounds_used": 0}
    cmd = asyncio.run(dr.assess_sufficiency(state, {"configurable": {"max_fact_rounds": 3,
                                                                     "database_path": str(tmp_path/'x.db')}}))
    assert cmd.goto == "write_research_brief"   # loops on error, does NOT finalize thin
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_assess_sufficiency.py::test_db_error_loops_instead_of_finishing -q -p no:warnings`
Expected: FAIL — current handler treats error as "sufficient" → `answer_from_facts`.

- [ ] **Step 3: Write minimal implementation**

In `assess_sufficiency`, change the except handler to set `missing = list(targets)` (still-missing) instead of logging "sufficient":

```python
        except Exception as e:
            logger.warning("assess_sufficiency check failed (treating as still-missing): %s", e)
            missing = list(targets)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_assess_sufficiency.py -q -p no:warnings`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/deep_researcher.py tests/test_assess_sufficiency.py
git commit -m "fix(graph): assess_sufficiency treats a DB error as still-missing, not sufficient"
```

---

## Final verification

- [ ] Run the full suite: `uv run pytest tests/ -q -p no:warnings` → all pass.
- [ ] Lint touched files: `uv run ruff check src/open_deep_research/ tests/ --output-format concise` → no new findings in changed lines.
- [ ] Live smoke (optional, paid): re-run one country end-to-end on a fresh budget and confirm: non-zero facts, no `done`-on-empty, and far fewer LLM calls than before. Brazil already validated the supervisor fix (0 → 32 facts).

## Self-review notes (done)

- Spec coverage: A1→Task A1; A2/A3→Task A2; A4→Task A3; B1/B2→Task B1; C1→C1; C2→C2; C3→C3; C4→C4; D1→B1+Task D1; D2→Task D2; E1→E1; E2→E2. All units covered.
- Type consistency: `default_run_one` returns the dict `{report_id, fact_count, status}` used by `batch.py` worker; `persist_research` returns the same keys consumed there; `_is_empty_run`, `_span_present`, `_run_fact_count`, `_raw_text_source_count` names are stable across tasks.
