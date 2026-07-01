# Search Read-Layer Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Four contained read-only fixes to the search layer: fact hits show a real value (not `None`), source `--subject` resolves from prompt-sentence names, the merged list ranks by a normalized cross-kind score, and searches leave `conn.row_factory` unchanged.

**Architecture:** All changes are in `factbase/search.py` (+ `factbase/store.py` for the `row_factory` restore in `read`). Read-only; no schema change, no migration.

**Tech Stack:** Python 3, `aiosqlite`, SQLite FTS5. Tests: `asyncio.run(...)` over `:memory:` DBs seeded through `migrations.apply` + `search_schema.ensure_search_schema` (house style).

## Global Constraints

- All four fixes live in `src/open_deep_research/factbase/search.py`; only Fix 4 also touches `RunSourceStore.read` in `src/open_deep_research/factbase/store.py`. No other file changes.
- Read-only + best-effort preserved: a malformed query still returns `[]`; no fix adds a new raise.
- `Hit.score` becomes a **normalized `[0,1]` per-kind rank** after Task 3 (was raw `−bm25`); update the docstring/comment accordingly.
- Interpreter: there is no bare `python`; run tests with `PYTHONPATH=src /mnt/c/Users/abradley/Projects/IdentityInnovation/search/open_deep_research/.venv/bin/python -m pytest …`.
- Out of scope: fixing the batch runner to store clean `subjects.name`; semantic/vector ranking; any schema change.

---

### Task 1: Empty-snippet fallback

**Files:**
- Modify: `src/open_deep_research/factbase/search.py` (`Hit`, `_fact_hits`, `format_hits`)
- Test: `tests/test_search_snippet_fallback.py`

**Interfaces:**
- Produces: `Hit.value: str | None`; a `fact` hit whose snippet is empty renders `"<property_name> = <value>"` in all formats.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_search_snippet_fallback.py
import asyncio, aiosqlite
from open_deep_research.factbase import schema, migrations, search_schema, search


async def _seed(conn):
    await conn.executescript("""
        CREATE TABLE subjects (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT, name TEXT);
        CREATE TABLE research_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id INTEGER, thread_id TEXT);
    """)
    await conn.commit()
    await migrations.apply(conn, schema.STEPS)
    await search_schema.ensure_search_schema(conn)
    # a fact whose value/property match "coverage" but with NULL narrative -> empty snippet
    await conn.execute("INSERT INTO fact (id, instance_key, property_name, value, narrative, "
                       "admission, lifecycle, soft_deleted_at) "
                       "VALUES (1,'EST','id_coverage_pct','98',NULL,'trusted','current',NULL)")
    await conn.commit()


def test_fact_hit_falls_back_to_value_when_snippet_empty():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            hits = await search.search_research(conn, "id_coverage_pct", kinds=("fact",))
            assert hits and hits[0].value == "98"
            out = search.format_hits(hits, "text")
            assert "id_coverage_pct = 98" in out
            assert "None" not in out
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/test_search_snippet_fallback.py -v`
Expected: FAIL — `Hit` has no `value`; output contains `None`.

- [ ] **Step 3: Add `Hit.value`, select it, and add the fallback**

In `search.py`: add to the `Hit` dataclass (after `admission`):
```python
    value: str | None = None
```
In `_fact_hits`, add `f.value` to the SELECT and set it on the Hit:
```python
        SELECT f.id, f.instance_key, f.property_name, f.value, f.as_of, f.lifecycle, f.admission,
```
```python
    return [Hit(kind="fact", ref_id=row["id"], subject=row["instance_key"],
                snippet=row["snip"], score=-row["score"], value=row["value"],
                property_name=row["property_name"], as_of=row["as_of"],
                lifecycle=row["lifecycle"], admission=row["admission"])
            for row in await cur.fetchall()]
```
In `format_hits`, add a snippet-fallback helper and use it everywhere the snippet is rendered:
```python
    def _snip(h):
        if h.snippet:
            return h.snippet
        return f"{h.property_name} = {h.value}" if h.kind == "fact" else (h.source_url or "(no snippet)")
```
Then replace `h.snippet` with `_snip(h)` in the csv row, the md `snip = (h.snippet or "")...` line (use `_snip(h)`), and the text line's trailing `{h.snippet}`.

- [ ] **Step 4: Run test to verify it passes**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 5: Run search regression**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/test_factbase_search.py tests/test_dossier_search_cli.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_search_snippet_fallback.py src/open_deep_research/factbase/search.py
git commit -m "fix(search): fall back to value/property when a hit snippet is empty"
```

---

### Task 2: Source `--subject` via `resolve_in_text`

**Files:**
- Modify: `src/open_deep_research/factbase/search.py` (`_source_hits`)
- Test: `tests/test_search_source_subject_resolve.py`

**Interfaces:**
- Produces: a source whose `subjects.name` is a prompt sentence resolves to the right alpha-3 for `--subject` filtering (via `CountryResolver().resolve_in_text`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_search_source_subject_resolve.py
import asyncio, aiosqlite
from open_deep_research.factbase import schema, migrations, search_schema, search


async def _seed(conn):
    await conn.executescript("""
        CREATE TABLE subjects (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT, name TEXT);
        CREATE TABLE research_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id INTEGER, thread_id TEXT);
    """)
    await conn.commit()
    await migrations.apply(conn, schema.STEPS)
    await search_schema.ensure_search_schema(conn)
    # subjects.name is a PROMPT SENTENCE (the real-data shape), not a clean country name
    await conn.execute("INSERT INTO subjects (id, slug, name) VALUES "
                       "(1,'x','Research Estonia for the country_digital_identity profile.')")
    await conn.execute("INSERT INTO research_runs (id, subject_id, thread_id) VALUES (1,1,'t1')")
    await conn.execute("INSERT INTO source_content (id, content_hash, source_url, title, text) "
                       "VALUES (1,'h1','https://ria.ee/roca','ROCA','ROCA vulnerability advisory')")
    await conn.execute("INSERT INTO run_source (thread_id, source_url, capture_status, content_hash) "
                       "VALUES ('t1','https://ria.ee/roca','raw_text','h1')")
    await conn.commit()


def test_source_subject_resolves_from_prompt_sentence_name():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            est = await search.search_research(conn, "ROCA", subject="Estonia", kinds=("source",))
            assert est and est[0].subject == "EST"
            deu = await search.search_research(conn, "ROCA", subject="Germany", kinds=("source",))
            assert deu == []
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/test_search_source_subject_resolve.py -v`
Expected: FAIL — `resolve("Research Estonia for …")` returns None, so the `--subject Estonia` filter excludes it (empty result).

- [ ] **Step 3: Use `resolve_in_text` for the source's subject name**

In `_source_hits` (`search.py`), change the subjects set comprehension from `resolve` to `resolve_in_text`:
```python
        subjects = {CountryResolver().resolve_in_text(n[0]) for n in await subj_cur.fetchall() if n[0]}
```
(Leave `_resolve_subject`, which resolves the user's clean `--subject` argument, on `resolve`.)

- [ ] **Step 4: Run test to verify it passes**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 5: Run search regression**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/test_factbase_search.py -q`
Expected: PASS (its seeds use clean names, which `resolve_in_text` also resolves).

- [ ] **Step 6: Commit**

```bash
git add tests/test_search_source_subject_resolve.py src/open_deep_research/factbase/search.py
git commit -m "fix(search): resolve source subject from prompt-sentence subjects.name (resolve_in_text)"
```

---

### Task 3: Cross-kind score normalization

**Files:**
- Modify: `src/open_deep_research/factbase/search.py` (`search_research`, add `_normalize`; update `Hit.score` comment + docstring)
- Test: `tests/test_search_score_normalization.py`

**Interfaces:**
- Produces: `search_research` normalizes each kind's scores to `[0,1]` (min-max; all-equal/single → `1.0`) before merging; the merged list sorts by the normalized `score`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_search_score_normalization.py
import asyncio, aiosqlite
from open_deep_research.factbase import schema, migrations, search_schema, search


async def _seed(conn):
    await conn.executescript("""
        CREATE TABLE subjects (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT, name TEXT);
        CREATE TABLE research_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id INTEGER, thread_id TEXT);
    """)
    await conn.commit()
    await migrations.apply(conn, schema.STEPS)
    await search_schema.ensure_search_schema(conn)
    # two facts + one source all matching "identity", different bm25
    await conn.execute("INSERT INTO fact (id, instance_key, property_name, value, narrative, soft_deleted_at) "
                       "VALUES (1,'EST','p','v','identity card scheme national identity',NULL)")
    await conn.execute("INSERT INTO fact (id, instance_key, property_name, value, narrative, soft_deleted_at) "
                       "VALUES (2,'EST','q','v','identity',NULL)")
    await conn.execute("INSERT INTO source_content (id, content_hash, source_url, title, text) "
                       "VALUES (1,'h','http://x','T','national digital identity wallet identity')")
    await conn.commit()


def test_scores_normalized_to_unit_range_per_kind():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            hits = await search.search_research(conn, "identity")
            assert hits
            assert all(0.0 <= h.score <= 1.0 for h in hits)          # normalized
            assert max(h.score for h in hits) == 1.0                 # top of each kind is 1.0
            # sorted descending by normalized score
            assert hits == sorted(hits, key=lambda h: h.score, reverse=True)
    asyncio.run(run())


def test_single_hit_kind_normalizes_to_one():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _seed(conn)
            hits = await search.search_research(conn, "wallet", kinds=("source",))
            assert len(hits) == 1 and hits[0].score == 1.0
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/test_search_score_normalization.py -v`
Expected: FAIL — raw `−bm25` scores are negative/out of `[0,1]`.

- [ ] **Step 3: Normalize per kind before merging**

In `search.py`, add the helper:
```python
def _normalize(hits: list) -> None:
    """Min-max scale hits' .score to [0,1] in place (all-equal / single -> 1.0)."""
    if not hits:
        return
    scores = [h.score for h in hits]
    lo, hi = min(scores), max(scores)
    for h in hits:
        h.score = 1.0 if hi == lo else (h.score - lo) / (hi - lo)
```
Rewrite `search_research`'s body (collect each kind separately, normalize, merge):
```python
    await search_schema.ensure_search_schema(conn)
    match = _to_match(query)
    if match is None:
        return []
    target = await _resolve_subject(subject)
    try:
        src = await _source_hits(conn, match, target, limit) if "source" in kinds else []
        fct = await _fact_hits(conn, match, target, limit) if "fact" in kinds else []
    except aiosqlite.OperationalError:
        return []  # FTS syntax edge cases degrade to "no results", never raise
    _normalize(src)
    _normalize(fct)
    hits = src + fct
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]
```
Update the `Hit.score` comment to `# normalized [0,1] per-kind rank (higher = better)` and the
`search_research` docstring line to "Scores are normalized per-kind to [0,1] before the merge."

- [ ] **Step 4: Run test to verify it passes**

Run the Step 2 command. Expected: PASS (both tests).

- [ ] **Step 5: Run search + CLI regression**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/test_factbase_search.py tests/test_dossier_search_cli.py -q`
Expected: PASS. (If a test asserts an exact raw `score` value, update it to the normalized expectation and note it in the commit.)

- [ ] **Step 6: Commit**

```bash
git add tests/test_search_score_normalization.py src/open_deep_research/factbase/search.py
git commit -m "fix(search): normalize cross-kind bm25 scores to [0,1] before merge"
```

---

### Task 4: Restore `conn.row_factory`

**Files:**
- Modify: `src/open_deep_research/factbase/search.py` (`_source_hits`, `_fact_hits`)
- Modify: `src/open_deep_research/factbase/store.py` (`RunSourceStore.read`)
- Test: `tests/test_search_row_factory_restore.py`

**Interfaces:**
- Produces: `search_research` and `RunSourceStore.read` leave `conn.row_factory` at its prior value.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_search_row_factory_restore.py
import asyncio, aiosqlite
from open_deep_research.factbase import schema, migrations, search_schema, search, store


async def _prep(conn):
    await conn.executescript("""
        CREATE TABLE subjects (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT, name TEXT);
        CREATE TABLE research_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id INTEGER, thread_id TEXT);
    """)
    await conn.commit()
    await migrations.apply(conn, schema.STEPS)
    await search_schema.ensure_search_schema(conn)


def test_search_restores_row_factory():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _prep(conn)
            conn.row_factory = None
            await search.search_research(conn, "anything")
            assert conn.row_factory is None            # restored, not left as aiosqlite.Row
    asyncio.run(run())


def test_read_restores_row_factory():
    async def run():
        async with aiosqlite.connect(":memory:") as conn:
            await _prep(conn)
            conn.row_factory = None
            await store.RunSourceStore(conn).read("t1")
            assert conn.row_factory is None
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/test_search_row_factory_restore.py -v`
Expected: FAIL — `conn.row_factory` is left as `aiosqlite.Row`.

- [ ] **Step 3: Save/restore in the three query functions**

In `search.py` `_source_hits`, wrap the body so the factory is restored:
```python
async def _source_hits(conn, match, target, limit):
    sql = f"""...unchanged..."""
    _prev = conn.row_factory
    conn.row_factory = aiosqlite.Row
    try:
        cur = await conn.execute(sql, (match, limit))
        rows = await cur.fetchall()
        out = []
        for row in rows:
            ...unchanged loop...
        return out
    finally:
        conn.row_factory = _prev
```
Do the same in `_fact_hits` (wrap the `conn.execute` + list-comprehension build in `try/finally`, restoring `_prev`). In `store.py` `RunSourceStore.read`, wrap identically:
```python
    async def read(self, thread_id: str) -> list[dict]:
        _prev = self._conn.row_factory
        self._conn.row_factory = aiosqlite.Row
        try:
            cur = await self._conn.execute(...unchanged SQL...)
            return [dict(r) for r in await cur.fetchall()]
        finally:
            self._conn.row_factory = _prev
```

- [ ] **Step 4: Run test to verify it passes**

Run the Step 2 command. Expected: PASS (both).

- [ ] **Step 5: Broad regression**

Run: `PYTHONPATH=src …/.venv/bin/python -m pytest tests/ -k "search or dossier or backfill or store or extraction" -q`
Expected: PASS (rows are still `aiosqlite.Row` inside each query; only the post-call factory is restored).

- [ ] **Step 6: Commit**

```bash
git add tests/test_search_row_factory_restore.py src/open_deep_research/factbase/search.py src/open_deep_research/factbase/store.py
git commit -m "fix(search): save/restore conn.row_factory in query paths"
```

---

## Self-Review

**Spec coverage:** Task 1 = empty-snippet fallback (Hit.value + _fact_hits + format_hits); Task 2 = source subject via resolve_in_text; Task 3 = per-kind min-max normalization; Task 4 = row_factory save/restore (search.py + store.py). All in `search.py`/`store.py`, read-only, no schema change (matches spec).

**Placeholder scan:** none — every step has full code/commands (`…/.venv/bin/python` abbreviates `/mnt/c/Users/abradley/Projects/IdentityInnovation/search/open_deep_research/.venv/bin/python`).

**Type consistency:** `Hit.value` (Task 1) is set in `_fact_hits` and read in `format_hits`; `_normalize(hits)` (Task 3) mutates `Hit.score`; `_source_hits`/`_fact_hits`/`read` all use the same `_prev`/`try/finally` restore (Task 4). Task 3 changes `Hit.score` semantics to `[0,1]` — the only consumer, `format_hits`, just displays it.

**Ordering note:** Task 3 (normalization) restructures `search_research`'s try/except; Task 4 (row_factory) wraps the inner query functions. They compose — do Task 3 before or after Task 4 without conflict (different regions).
