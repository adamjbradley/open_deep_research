# Exa + Hybrid Search Backend — Design

**Date:** 2026-06-19
**Status:** Approved (design); pending implementation plan
**Context:** Empirical probe (this session) showed Exa beats tavily on relevance for the
digital-identity domain (mean 7.83 vs 6.37, winning/tying all 6 queries, biggest margins on
hard/specific queries) AND that the two **barely overlap** (~0.5/5 URLs per query) — they are
complementary, not redundant. This adds Exa as a search backend and a tavily+Exa hybrid that
unions both, behind the existing factbase ingestion path.

## Problem

`get_search_tool(search_api)` offers `tavily` (the only retrieval API returning raw per-source
content, which `tavily_search` persists via `record_search_sources` → `run_source` →
`extract_facts`) plus agentic platform searches (claude/gemini/codex/anthropic/openai) that
return *digested* text and so don't fit per-source extraction. Exa — a neural/semantic retrieval
API returning raw content + query-focused summaries — is a dependency (`exa-py`) but only wired
in `src/legacy/`. The probe shows Exa is a relevance upgrade and complements tavily, so neither
alone is optimal.

## Goals

Expose **`exa`** (standalone) and **`tavily_exa`** (hybrid union) as selectable search backends,
both feeding the factbase identically to tavily, with the hybrid merge **cost-neutral**
(same summarization volume as today).

## Section 1 — Architecture: split acquire / finalize

`tavily_search`'s tail (dedup → `record_search_sources` → summarize → format) is backend-agnostic
(operates on a `unique_results` dict). Extract it; make acquisition pluggable:

```
_finalize_search(unique_results, topic, config) -> str   # shared tail, extracted from tavily_search
_acquire_tavily(queries, n, topic, config) -> dict[url, {url,title,content,raw_content,query}]
_acquire_exa(queries, n, topic, config)    -> dict   # same normalized shape
_acquire_hybrid(queries, n, topic, config) -> dict   # interleave tavily+exa, dedup, cap at n
```
Three thin `@tool`s = acquire + `_finalize`: `tavily_search` (behavior unchanged), `exa_search`,
`tavily_exa_search`. **Invariant:** every acquirer emits the same normalized per-source dict
(`url, title, content, raw_content, query`), so `_finalize`/`record_search_sources` stay
backend-blind and the summarization cost controls (summary cache, `summarize_search_results`
toggle, `_summarize_semaphore`) apply uniformly.

## Section 2 — Exa acquirer + normalization

```python
def get_exa_api_key(config) -> str | None:   # mirrors get_tavily_api_key:
    # config["configurable"]["apiKeys"]["EXA_API_KEY"] else os.getenv("EXA_API_KEY")

async def _acquire_exa(queries, n, topic, config) -> dict:
    exa = Exa(api_key=get_exa_api_key(config))
    # per query, in a thread executor (exa_py is sync), bounded by EXA_TIMEOUT/CLI_BACKEND_TIMEOUT:
    #   exa.search_and_contents(q, text={"max_characters": max_content_length},
    #                           summary=True, num_results=n)
    # gather across queries; dedup by URL; normalize each result to:
    #   {url, title: r.title, content: r.summary, raw_content: r.text, query: q}
    return unique_results
```
- **Normalization (the crux):** `raw_content ← r.text` (full text → summarize/extract), `content ← r.summary` (query-focused snippet → used when `summarize_search_results=False` and as fallback), `title ← r.title`, `query ← q`.
- **Topic:** Exa has no `general/news/finance` equivalent; the acquirer ignores `topic` (non-goal, not faked).
- **Errors:** any Exa failure (missing key, API/timeout) is caught, logged, returns `{}` (best-effort) — so `exa_search` degrades to empty and the hybrid degrades to tavily-only.

## Section 3 — Hybrid merge (interleaved, capped, auto-degrading)

```python
async def _acquire_hybrid(queries, n, topic, config) -> dict:
    tav, exa = await asyncio.gather(_acquire_tavily(...), _acquire_exa(...))   # concurrent
    merged, seen = {}, set()
    tav_list, exa_list = list(tav.values()), list(exa.values())
    for i in range(max(len(tav_list), len(exa_list))):
        for src in (exa_list[i:i+1] + tav_list[i:i+1]):   # Exa first (higher probe relevance)
            if src["url"] not in seen and len(merged) < n:
                seen.add(src["url"]); merged[src["url"]] = src
    return merged
```
- **Interleaved, capped at `max_search_results`** (`n`) — cost-neutral (same summarization volume as today); slots filled best-of-both, Exa-first. Each backend's own ranking preserved.
- **Graceful degradation falls out:** Exa errored → empty `exa_list` → top-`n` tavily (today's behavior); tavily errored → top-`n` Exa; only empty if both fail.
- Merged dict → same `_finalize` → `record_search_sources` + summarize + format.
- Consequence (intended): cap-at-`n` yields **diversity, not more sources** — ~half Exa/half tavily per query at flat cost. (Full-recall would raise `n`; cost-neutral was chosen.)

## Section 4 — Config & routing wiring

- `SearchAPI` (`configuration.py`): add `EXA = "exa"`, `TAVILY_EXA = "tavily_exa"`.
- `get_search_tool`: `EXA → [exa_search]`, `TAVILY_EXA → [tavily_exa_search]` (with the
  `metadata={type:search,name:web_search}` treatment like tavily).
- `KNOWN_SEARCH` (`model_routing.py`): add `"exa"`, `"tavily_exa"`.
- `get_exa_api_key` helper; `.env.example` documents `EXA_API_KEY` (free tier at exa.ai). `exa-py`
  already a dependency.
- Config UI options list (`configuration.py`): add "Exa (neural)" + "Tavily + Exa (hybrid)".
- **Default unchanged:** `search` stays `"tavily"`; `exa`/`tavily_exa` are opt-in via a preset's
  `search` value (no preset auto-switches). `tavily_exa` is the recommended setting for factbase
  runs (relevance + diversity at flat cost).

## Section 5 — Testing

**Deterministic (mock clients, no live API):**
- `_acquire_exa` — fake Exa client → normalized dicts (`raw_content←text`, `content←summary`,
  `query` set); an Exa exception → `{}`.
- `_acquire_hybrid` — fake tavily/exa acquirers: interleaves Exa-first, dedups by URL, caps at
  `n`; exa-empty → tavily-only; tavily-empty → exa-only.
- `_finalize` regression — extracted tail still calls `record_search_sources` and formats; an
  existing tavily-path test passes unchanged.
- `get_search_tool` returns `exa_search`/`tavily_exa_search` for the new enums; `KNOWN_SEARCH`
  validates a preset with `search: "tavily_exa"`.

**Empirical (harness exists):** rerun the session's tavily-vs-exa comparison through the wired
`get_search_tool` path to confirm parity, and optionally judge the *hybrid* output.

## Files touched (anticipated)

- `utils.py` — `_finalize_search`, `_acquire_tavily`/`_acquire_exa`/`_acquire_hybrid`,
  `exa_search`/`tavily_exa_search` tools, `get_exa_api_key`; refactor `tavily_search`.
- `configuration.py` — `SearchAPI` EXA/TAVILY_EXA + UI options + `get_search_tool` dispatch.
- `model_routing.py` — `KNOWN_SEARCH` += exa/tavily_exa.
- `.env.example` — `EXA_API_KEY`.
- Tests alongside existing search/config tests.

## Non-goals

- Not changing the agentic platform searches (claude/gemini/codex/anthropic/openai).
- Not auto-switching any preset's `search` (operator opt-in).
- Not mapping Exa categories/topic, subpages, or domain filters (YAGNI; add later if needed).
- Not the full-recall (raise-`n`) merge — cost-neutral cap chosen.

## Risks / open questions

- **Probe relevance confound:** the judge scored title+snippet, and Exa's snippet is its
  query-focused summary vs tavily's generic excerpt — so Exa's measured edge may partly be
  snippet quality. The hybrid sidesteps this (it uses both); a fuller-content rejudge is optional.
- **Cost-neutral cap reduces recall vs the probe's union:** the biggest finding (near-zero
  overlap → ~2× unique) is only partly captured at cap-`n`. If recall matters more later, raising
  `n` for `tavily_exa` is a one-line change (revisit with cost data).
- **Exa free-tier limits:** 1000 searches/mo; a heavy fan-out run could exhaust it → the hybrid
  then degrades to tavily-only (acceptable, by design).
