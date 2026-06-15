# Living Fact Base — Implementation Plan (Plan 5: Discover-then-Fetch Evidence Capture)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the fact base populate on **any** search backend (including the default summarizing Claude/Gemini/Codex web search) by decoupling *discovery* from *evidence capture*: harvest the URLs a run cited, independently fetch each one's raw text into `run_source`, then extract + span-verify against that — preserving per-source provenance and the anti-hallucination guard.

**Architecture:** Extends Architecture v6 §2 (the `run_source` capture path). Today only `tavily_search` writes raw `run_source` rows (tool layer); summarizing backends record nothing usable, so `extract_facts` produces zero facts. Plan 5 adds a **backfill step** inside `extract_facts`: discover cited URLs from the run output → fetch the ones not already captured → record their text as `run_source` → existing extraction/ingestion is unchanged. The model never asserts a fact from its own summary; values are still verified against independently-fetched source text.

**Tech Stack:** Python 3.11, `httpx` + `beautifulsoup4` (both already deps), `aiosqlite`, `pytest` (`uv run pytest`; sync tests use `asyncio.run()`). NO new dependencies.

**Scope:** the backfill fetcher + its wiring. Honest limit (documented, not hidden): pages that are paywalled, JS-rendered, binary, or blocked won't yield text → those sources are recorded `skipped` and the run is flagged `coverage_incomplete`. Robots.txt politeness, JS rendering, and per-domain rate-limit tuning are v1.2.

**Grounding (verified):** deps `httpx>=0.24.0`, `beautifulsoup4==4.14.3`, `markdownify`, `aiohttp` all present (pyproject/uv.lock). `storage.extract_sources(*texts)` regex-greps de-duplicated URLs from text blobs. `extract_facts` (deep_researcher.py) already reads `run_source` by thread_id and runs `FactExtractor`→`Ingestor`; `AgentState` has `final_report`/`raw_notes`/`notes`. `RunSourceStore.record(thread_id, url, text, capture_status=)` + `.read(thread_id)` exist; `record` dedups on (thread_id, url, content_hash).

---

### Task 1: URL fetcher (`fetch.py`)

**Files:**
- Create: `src/open_deep_research/factbase/fetch.py`
- Test: `tests/test_factbase_fetch.py`

**Context:** `fetch_text(url, *, client=None, timeout=10.0, max_bytes=2_000_000) -> str | None`. Best-effort: GET via httpx (injectable `client` for tests), reject non-HTML/text content-types and oversize bodies, strip HTML to readable text via BeautifulSoup, return `None` on ANY error. The pure HTML→text helper `html_to_text(html)` is separately testable.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_fetch.py
import asyncio
from open_deep_research.factbase import fetch

def test_html_to_text_strips_markup_and_scripts():
    html = "<html><head><style>x{}</style></head><body><script>bad()</script>" \
           "<h1>India</h1><p>coverage was 99% among adults</p></body></html>"
    txt = fetch.html_to_text(html)
    assert "coverage was 99% among adults" in txt
    assert "bad()" not in txt and "x{}" not in txt

def test_fetch_text_uses_injected_client_and_returns_text():
    class _Resp:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}
        content = b"<html><body><p>India coverage 99%</p></body></html>"
        text = "<html><body><p>India coverage 99%</p></body></html>"
    class _Client:
        async def get(self, url, **kw): return _Resp()
        async def aclose(self): pass
    out = asyncio.run(fetch.fetch_text("https://x.org/a", client=_Client()))
    assert "India coverage 99%" in out

def test_fetch_text_rejects_non_html_content_type():
    class _Resp:
        status_code = 200
        headers = {"content-type": "application/pdf"}
        content = b"%PDF-1.4 ..."
        text = ""
    class _Client:
        async def get(self, url, **kw): return _Resp()
        async def aclose(self): pass
    assert asyncio.run(fetch.fetch_text("https://x.org/a.pdf", client=_Client())) is None

def test_fetch_text_returns_none_on_error():
    class _Client:
        async def get(self, url, **kw): raise RuntimeError("boom")
        async def aclose(self): pass
    assert asyncio.run(fetch.fetch_text("https://x.org/a", client=_Client())) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_factbase_fetch.py -v`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement**

```python
# src/open_deep_research/factbase/fetch.py
"""Best-effort URL → readable text fetch for evidence backfill.

Used to independently retrieve the raw text of sources a run cited, so facts can
be span-verified against real source text rather than the model's summary.
NEVER raises: returns None on any failure (timeout, non-HTML, oversize, network).
"""
from __future__ import annotations
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_OK_TYPES = ("text/html", "text/plain", "application/xhtml")


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    return " ".join(text.split())


async def fetch_text(url: str, *, client=None, timeout: float = 10.0,
                     max_bytes: int = 2_000_000) -> str | None:
    if not (url or "").lower().startswith(("http://", "https://")):
        return None
    own = client is None
    if own:
        import httpx
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                   headers={"User-Agent": "open-deep-research-factbase/1.0"})
    try:
        resp = await client.get(url)
        if getattr(resp, "status_code", 0) != 200:
            return None
        ctype = (resp.headers.get("content-type") or "").lower()
        if not any(t in ctype for t in _OK_TYPES):
            return None
        if len(getattr(resp, "content", b"") or b"") > max_bytes:
            return None
        text = fetch_text_from_response(resp)
        return text or None
    except Exception as e:
        logger.warning("fetch_text failed for %s: %s", url, e)
        return None
    finally:
        if own:
            try:
                await client.aclose()
            except Exception:
                pass


def fetch_text_from_response(resp) -> str:
    return html_to_text(getattr(resp, "text", "") or "")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_factbase_fetch.py -v`
Expected: PASS (all 4).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/fetch.py tests/test_factbase_fetch.py
git commit -m "feat(factbase): best-effort URL->text fetcher (httpx + bs4, injectable)"
```

---

### Task 2: Backfill `run_source` from cited URLs

**Files:**
- Create: `src/open_deep_research/factbase/backfill.py`
- Test: `tests/test_factbase_backfill.py`

**Context:** `backfill_run_sources(store, thread_id, urls, fetcher, *, max_urls=20) -> dict` — for each URL (deduped, capped) NOT already recorded for this thread, call `fetcher(url)`; record `raw_text` if text returned, else `skipped`. The `fetcher` is injected (so tests use a stub; production passes `fetch.fetch_text`). Returns counts `{fetched, skipped}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factbase_backfill.py
import asyncio, aiosqlite
from open_deep_research.factbase import migrations, schema, store, backfill

def test_backfill_records_fetched_and_skipped():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            rs = store.RunSourceStore(conn)
            # 'a' fetches text; 'b' fails (fetcher returns None)
            async def fetcher(url):
                return "India coverage 99% among adults" if url.endswith("/a") else None
            res = await backfill.backfill_run_sources(rs, "t1",
                ["https://x.org/a", "https://y.org/b", "https://x.org/a"],  # dup a
                fetcher)
            assert res == {"fetched": 1, "skipped": 1}
            rows = {r["source_url"]: r for r in await rs.read("t1")}
            assert rows["https://x.org/a"]["capture_status"] == "raw_text"
            assert rows["https://x.org/a"]["text"].startswith("India coverage")
            assert rows["https://y.org/b"]["capture_status"] == "skipped"
    asyncio.run(run())

def test_backfill_skips_urls_already_captured():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            rs = store.RunSourceStore(conn)
            await rs.record("t1", "https://x.org/a", "ALREADY HERE", capture_status="raw_text")
            calls = []
            async def fetcher(url):
                calls.append(url); return "new"
            res = await backfill.backfill_run_sources(rs, "t1", ["https://x.org/a"], fetcher)
            assert calls == []                 # already captured -> not re-fetched
            assert res["fetched"] == 0
    asyncio.run(run())

def test_backfill_caps_url_count():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await conn.executescript("CREATE TABLE research_runs (id INTEGER PRIMARY KEY, topic TEXT);")
            await conn.commit()
            await migrations.apply(conn, schema.STEPS)
            rs = store.RunSourceStore(conn)
            n = []
            async def fetcher(url):
                n.append(url); return "t"
            await backfill.backfill_run_sources(rs, "t1",
                [f"https://x.org/{i}" for i in range(50)], fetcher, max_urls=10)
            assert len(n) == 10
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_factbase_backfill.py -v`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Implement**

```python
# src/open_deep_research/factbase/backfill.py
"""Backfill run_source by independently fetching cited URLs' raw text.

Decouples discovery (whatever search the run used) from evidence capture, so facts
can be span-verified against independently-fetched source text on ANY search backend.
"""
from __future__ import annotations


async def backfill_run_sources(store, thread_id: str, urls: list[str], fetcher,
                               *, max_urls: int = 20) -> dict:
    existing = {r["source_url"] for r in await store.read(thread_id)}
    seen: set[str] = set()
    fetched = skipped = 0
    for url in urls:
        if fetched + skipped >= max_urls:
            break
        if not url or url in seen or url in existing:
            continue
        seen.add(url)
        text = await fetcher(url)
        if text:
            await store.record(thread_id, url, text, capture_status="raw_text")
            fetched += 1
        else:
            await store.record(thread_id, url, None, capture_status="skipped")
            skipped += 1
    return {"fetched": fetched, "skipped": skipped}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_factbase_backfill.py -v`
Expected: PASS (all 3).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/factbase/backfill.py tests/test_factbase_backfill.py
git commit -m "feat(factbase): backfill run_source from cited URLs (fetcher injected, capped, dedup)"
```

---

### Task 3: Wire backfill into `extract_facts` (discover → fetch → extract)

**Files:**
- Modify: `src/open_deep_research/deep_researcher.py` (`extract_facts`)
- Test: `tests/test_extract_facts_backfill_wiring.py`

**Context:** Before reading `run_source`, `extract_facts` harvests cited URLs from the run output (`final_report` + `raw_notes` via `storage.extract_sources`) and backfills any not already captured (using `fetch.fetch_text`). Then the existing read→extract→ingest runs unchanged. Best-effort: failure logs, never breaks the run. This makes facts populate regardless of search backend.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_extract_facts_backfill_wiring.py
import asyncio
from open_deep_research import deep_researcher as dr

def test_extract_facts_harvests_urls_and_backfills(monkeypatch, tmp_path):
    db = str(tmp_path / "f.db")
    # stub the fetcher so no real network: any url -> text containing a verifiable span
    async def fake_fetch(url, **kw):
        return "India: coverage was 99% among adults in 2024."
    monkeypatch.setattr(dr, "_fact_fetch_text", fake_fetch, raising=False)
    # stub the extraction model_call to emit a fact whose span is in the fetched text
    async def fake_model_call_factory(configurable, config):
        async def _call(text, prof):
            return [{"property":"id_coverage_pct","instance_name":"India","value":"99","unit":"%",
                     "as_of":"2024","qualifiers":{"population_basis":"adults_15plus"},
                     "evidence_span":"coverage was 99% among adults"}]
        return _call
    monkeypatch.setattr(dr, "_make_fact_model_call", fake_model_call_factory, raising=False)

    from langchain_core.runnables import RunnableConfig
    from open_deep_research import storage
    # Seed the DB the way the graph would (preallocate_run runs _ensure_schema + migrations,
    # creates the research_runs row, and returns its id). Do NOT hardcode the id on a bare DB —
    # extract_facts only runs migrations.apply, and v2's ALTER needs research_runs to exist.
    rid = asyncio.run(storage.preallocate_run(db, "t-bf"))
    state = {"final_report": "See https://id4d.worldbank.org/india for details.",
             "raw_notes": [], "prealloc_run_id": rid}
    cfg = RunnableConfig(configurable={"persist_results": True, "thread_id": "t-bf",
                                       "database_path": db})
    asyncio.run(dr.extract_facts(state, cfg))

    import aiosqlite
    async def check():
        async with aiosqlite.connect(db) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM fact")
            return (await cur.fetchone())[0]
    assert asyncio.run(check()) >= 1   # a fact was produced from the BACKFILLED source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_extract_facts_backfill_wiring.py -v`
Expected: FAIL (extract_facts doesn't backfill / `_fact_fetch_text` indirection absent).

- [ ] **Step 3: Implement**

In `deep_researcher.py`, add a module-level indirection so tests can stub the fetcher:
```python
from open_deep_research.factbase import fetch as _fb_fetch
async def _fact_fetch_text(url, **kw):
    return await _fb_fetch.fetch_text(url, **kw)
```
Inside `extract_facts`, after opening the conn and BEFORE `sources = await ...read(thread_id)`, add the discover→backfill step (inside the existing try/except):
```python
        from open_deep_research.factbase import backfill as _fb_backfill
        from open_deep_research.storage import extract_sources as _extract_sources
        # Discover cited URLs from the run output and independently fetch their text
        # so facts can be span-verified on ANY search backend (not just Tavily).
        cited = _extract_sources(state.get("final_report", ""), *(state.get("raw_notes", []) or []))
        if cited:
            await _fb_backfill.backfill_run_sources(
                store.RunSourceStore(conn), str(thread_id), cited, _fact_fetch_text)
```
(`store`, `get_db_path`, `configurable`, `thread_id`, `run_id` are already in scope from the existing extract_facts body. The subsequent `read`/extract/ingest is unchanged and now sees the backfilled raw_text rows. Keep it all inside the existing best-effort try/except.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_extract_facts_backfill_wiring.py -v`
Expected: PASS. Then `uv run pytest tests/test_graph_extract_facts_wiring.py -v` (existing extract_facts tests still pass) and `uv run python -c "import open_deep_research.deep_researcher; print('compiles')"`.

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/deep_researcher.py tests/test_extract_facts_backfill_wiring.py
git commit -m "feat(factbase): extract_facts discovers + fetches cited URLs (any search backend)"
```

---

### Task 4: Full-suite check + scope doc

**Files:** (none new)

- [ ] **Step 1:** `uv run pytest -q -p no:warnings` → all pass.
- [ ] **Step 2:** `uv run python -c "import open_deep_research.deep_researcher; print('compiles')"` → `compiles`.
- [ ] **Step 3:** Confirm the Tavily path is unaffected: `tavily_search` still captures at the tool layer; backfill only fills *un-captured* URLs, so Tavily runs aren't double-fetched (the `existing` check in backfill).

---

## Plan 5 complete

After Task 4: a research run on **any** search backend (Claude/Gemini/Codex web search, or Tavily) populates the fact base — the run discovers sources, we independently fetch each cited URL's raw text, and facts are extracted + span-verified against that text. Provenance stays per-source; the anti-hallucination guard holds (no fact survives without a verbatim span in independently-fetched text). The model's summary is used only for *discovery*, never as the asserted fact.

**Honest limits (documented):**
- Paywalled / JS-rendered / binary / blocked pages fetch nothing → recorded `skipped` → run flagged `coverage_incomplete` (visible in `dossier stats`/the run row). No silent gaps.
- No robots.txt handling, JS rendering, or per-domain rate limiting yet (v1.2).
- The fetch adds network I/O per run (capped at `max_urls`, default 20, best-effort with timeouts).

**Still v1.1+ (carried):** wire/schedule the reaper + refresh `last_heartbeat`; registry-version recompute pass; calibrate the extraction prompt against real runs; cosmetic minors. **And the live end-to-end validation** — now runnable on the default Claude-search backend once model auth is configured (no Tavily key required), since backfill supplies the source text.
