# agy (Antigravity CLI) Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a unified `agy:` CLI backend (the working Antigravity replacement for the dead gemini CLI) exposing Gemini 3.x / Claude 4.6 / GPT-OSS via stable tier-explicit slugs, with the silent-default footgun closed.

**Architecture:** A dedicated `AgyCLIChat(_CLIJsonChat)` mirrors `GeminiCLIChat` but invokes `agy` without `-o json` (and without auto tool-approval — security), mapping our slugs to agy's exact display names via `to_agy_model` (unknown slug raises). The `agy:` prefix routes through `build_chat_model`; `agy` is a known backend with `backends.agy` settings and an `agy` preset; preflight probes the binary.

**Tech Stack:** Python 3.11, pydantic v2, pytest, the existing CLI-backend machinery in `claude_agent_chat.py` (`_CLIJsonChat`, `_invoke`), `model_routing.py`, `preflight.py`. Spec: `docs/superpowers/specs/2026-06-19-agy-backend-design.md`.

## Global Constraints

- Tests run with `.venv/bin/python -m pytest` (bare `python` is not on PATH).
- Already on branch `harden-routing-failover`; do NOT branch or touch main.
- **Additive only:** do NOT change the existing `gemini`/`codex`/`claude` backends. agy is a new, separate backend/class.
- agy invocation: **no `-o json`** (agy rejects `-o`); prompt via **stdin**; **no `--dangerously-skip-permissions` default** (security — opt-in via `AGY_CLI_ARGS` only in a sandbox); `_subprocess_env` scrubs app secrets (ANTHROPIC/OPENAI/GOOGLE/GEMINI/TAVILY keys), passes the rest through.
- **Unknown slug must raise `ValueError`** in `to_agy_model` — never let agy silently default to Gemini 3.5 Flash.
- `"agy"` must be added to BOTH `KNOWN_PREFIXES` and `KNOWN_BACKENDS` BEFORE the routing JSON references it (Task 3 precedes Task 4).
- Do NOT change `active_preset` (it stays `claude`; the `agy` preset is opt-in).

---

### Task 1: `to_agy_model` slug→display-name mapping

**Files:**
- Modify: `src/open_deep_research/claude_agent_chat.py` (add near `to_gemini_model`)
- Test: `tests/test_agy_backend.py`

**Interfaces:**
- Produces: `to_agy_model(slug: str) -> str` — maps a slug (optionally `agy:`-prefixed) to agy's exact display name; raises `ValueError` on an unknown slug. Module dict `_AGY_MODELS`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_agy_backend.py
import pytest
from open_deep_research.claude_agent_chat import to_agy_model

def test_to_agy_model_maps_known_slugs():
    assert to_agy_model("gemini-3.5-flash-high") == "Gemini 3.5 Flash (High)"
    assert to_agy_model("gemini-3.1-pro-low") == "Gemini 3.1 Pro (Low)"
    assert to_agy_model("claude-opus-4.6") == "Claude Opus 4.6 (Thinking)"
    assert to_agy_model("gpt-oss-120b") == "GPT-OSS 120B (Medium)"

def test_to_agy_model_strips_agy_prefix():
    assert to_agy_model("agy:gemini-3.5-flash-medium") == "Gemini 3.5 Flash (Medium)"

def test_to_agy_model_unknown_slug_raises():
    with pytest.raises(ValueError):
        to_agy_model("gemini-2.5-flash")        # not an agy slug -> must NOT silently default
    with pytest.raises(ValueError):
        to_agy_model("")
```

- [ ] **Step 2: Run tests, verify fail**

Run: `.venv/bin/python -m pytest tests/test_agy_backend.py -k to_agy_model -v`
Expected: FAIL — `to_agy_model` undefined.

- [ ] **Step 3: Implement `to_agy_model` (claude_agent_chat.py, near `to_gemini_model`)**

```python
# agy's --model requires the EXACT display-name string; an unrecognized id silently defaults
# to Gemini 3.5 Flash, so we map our stable tier-explicit slugs and RAISE on anything else.
_AGY_MODELS = {
    "gemini-3.5-flash-low":    "Gemini 3.5 Flash (Low)",
    "gemini-3.5-flash-medium": "Gemini 3.5 Flash (Medium)",
    "gemini-3.5-flash-high":   "Gemini 3.5 Flash (High)",
    "gemini-3.1-pro-low":      "Gemini 3.1 Pro (Low)",
    "gemini-3.1-pro-high":     "Gemini 3.1 Pro (High)",
    "claude-opus-4.6":         "Claude Opus 4.6 (Thinking)",
    "claude-sonnet-4.6":       "Claude Sonnet 4.6 (Thinking)",
    "gpt-oss-120b":            "GPT-OSS 120B (Medium)",
}


def to_agy_model(slug: str) -> str:
    """Map a stable agy slug (optionally 'agy:'-prefixed) to agy's exact --model display name.

    Raises ValueError on an unknown slug: agy silently defaults unrecognized ids to Gemini 3.5
    Flash, so a typo must fail loudly rather than mis-route.
    """
    s = (slug or "").strip()
    if s.lower().startswith("agy:"):
        s = s.split(":", 1)[1].strip()
    try:
        return _AGY_MODELS[s.lower()]
    except KeyError:
        raise ValueError(
            f"unknown agy model slug {slug!r}; known: {sorted(_AGY_MODELS)}"
        ) from None
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/python -m pytest tests/test_agy_backend.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/claude_agent_chat.py tests/test_agy_backend.py
git commit -m "feat(agy): to_agy_model slug->display-name mapping (unknown slug raises)"
```

---

### Task 2: `AgyCLIChat` backend class + `agy:` routing

**Files:**
- Modify: `src/open_deep_research/claude_agent_chat.py` (`AgyCLIChat` after `GeminiCLIChat`; `_BACKEND_PREFIXES`; `build_chat_model`)
- Test: `tests/test_agy_backend.py`

**Interfaces:**
- Consumes: `to_agy_model` (Task 1), `_CLIJsonChat`, `_combine_system_prompt`, `_invoke`.
- Produces: `AgyCLIChat(_CLIJsonChat)`; `build_chat_model("agy:<slug>")` returns an `AgyCLIChat` whose `self.model` is the agy display name.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_agy_backend.py (add)
import asyncio
from open_deep_research.claude_agent_chat import build_chat_model, AgyCLIChat

def test_agy_prefix_builds_agy_backend_with_display_name():
    m = build_chat_model("agy:gemini-3.1-pro-high")
    assert isinstance(m, AgyCLIChat)
    assert m.model == "Gemini 3.1 Pro (High)"     # mapped to the display name

def test_agy_command_has_no_o_json_and_skip_permissions(monkeypatch):
    m = build_chat_model("agy:gemini-3.5-flash-high")
    captured = {}
    async def fake_invoke(cmd, stdin=None):
        captured["cmd"] = cmd; captured["stdin"] = stdin
        return '[{"ok": true}]'
    monkeypatch.setattr(m, "_invoke", fake_invoke)
    asyncio.run(m._backend_generate("sys", "hello", None))
    assert captured["cmd"][:3] == ["agy", "--model", "Gemini 3.5 Flash (High)"]
    assert "--dangerously-skip-permissions" in captured["cmd"]
    assert "-o" not in captured["cmd"] and "json" not in captured["cmd"]
    assert captured["stdin"] is not None      # prompt via stdin
```

- [ ] **Step 2: Run tests, verify fail**

Run: `.venv/bin/python -m pytest tests/test_agy_backend.py -k "agy_prefix or agy_command" -v`
Expected: FAIL — `AgyCLIChat` undefined / `agy:` not routed.

- [ ] **Step 3: Add `AgyCLIChat` (after `GeminiCLIChat` in claude_agent_chat.py)**

```python
class AgyCLIChat(_CLIJsonChat):
    """LLM backend driven by Google's ``agy`` CLI (the Antigravity replacement for ``gemini``).

    Exposes agy's Gemini 3.x / Claude 4.6 / GPT-OSS models. agy rejects ``-o json`` and its
    plain output is already clean, so we invoke it without it; ``--dangerously-skip-permissions``
    auto-approves tools non-interactively. Like the gemini CLI it has no native schema flag, so
    structured output is coerced via the JSON envelope in the prompt (the _CLIJsonChat base).
    ``self.model`` is already the agy display name (mapped by ``to_agy_model`` in build_chat_model).
    """

    _backend_name = "agy-cli"

    def _subprocess_env(self) -> dict:
        # agy authenticates via the Antigravity login store, not GEMINI_API_KEY -- pass the
        # environment through unchanged (do NOT blank credentials).
        return dict(os.environ)

    async def _backend_generate(self, system_prompt, prompt, schema):
        if schema is not None:
            prompt = (
                prompt
                + "\n\nReturn ONLY a single JSON object matching this schema "
                "(no markdown fences, no commentary):\n"
                + json.dumps(schema)
            )
        full = _combine_system_prompt(system_prompt, prompt)
        bin_ = os.getenv("AGY_CLI_BIN", "agy")
        extra = os.getenv("AGY_CLI_ARGS", "--dangerously-skip-permissions").split()
        cmd = [bin_, "--model", self.model, *extra]   # NO -o json (agy rejects it)
        raw = await self._invoke(cmd, stdin=full)
        if "### Summary" in raw:                       # agy occasionally appends a Summary section
            raw = raw.split("### Summary")[0].strip()
        return raw, None
```

- [ ] **Step 4: Route `agy:` (claude_agent_chat.py)**

In `_BACKEND_PREFIXES` add `"agy": "agy"`. In `parse_backend`, after the `:`-prefix block already handles it via `_BACKEND_PREFIXES`; add a bare-keyword fallback before the final return: `if low.startswith("agy"): return "agy", s`. In `build_chat_model`, add (before the gemini branch or alongside):
```python
    if backend == "agy":
        return AgyCLIChat(model=to_agy_model(model), max_tokens=max_tokens, subscription=subscription)
```

- [ ] **Step 5: Run tests, verify pass**

Run: `.venv/bin/python -m pytest tests/test_agy_backend.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/open_deep_research/claude_agent_chat.py tests/test_agy_backend.py
git commit -m "feat(agy): AgyCLIChat backend + agy: routing (no -o json, skip-permissions)"
```

---

### Task 3: Routing recognition — `agy` known prefix/backend + `apply_backend_env`

**Files:**
- Modify: `src/open_deep_research/model_routing.py` (`KNOWN_PREFIXES`, `KNOWN_BACKENDS`, `apply_backend_env`)
- Test: `tests/test_model_routing_backend_env.py`

**Interfaces:**
- Produces: `"agy"` accepted in model strings and as a backend; `apply_backend_env` pushes `AGY_CLI_BIN`/`AGY_CLI_ARGS` from `backends.agy`.

- [ ] **Step 1: Write failing test**

```python
# tests/test_model_routing_backend_env.py (add)
def test_agy_backend_env_pushed(monkeypatch):
    for k in ("AGY_CLI_BIN", "AGY_CLI_ARGS"):
        monkeypatch.delenv(k, raising=False)
    from open_deep_research.model_routing import routing_from_dict, apply_backend_env
    r = routing_from_dict({
        "version": "1", "active_preset": "p",
        "backends": {"agy": {"cli_bin": "agy", "cli_args": ["--print-timeout", "600"]}},
        "presets": {"p": {"roles": {"researcher": "agy:gemini-3.5-flash-high"}, "search": "tavily"}},
    })
    apply_backend_env(r)
    import os
    assert os.environ["AGY_CLI_BIN"] == "agy"
    assert os.environ["AGY_CLI_ARGS"] == "--print-timeout 600"   # args pushed (no skip-permissions)
```

- [ ] **Step 2: Run test, verify fail**

Run: `.venv/bin/python -m pytest tests/test_model_routing_backend_env.py -k agy -v`
Expected: FAIL — `"agy"` rejected as an unknown backend prefix / `apply_backend_env` doesn't push agy.

- [ ] **Step 3: Implement (model_routing.py)**

Add `"agy"` to `KNOWN_PREFIXES` and `KNOWN_BACKENDS`:
```python
KNOWN_BACKENDS = {"claude", "gemini", "codex", "agy"}
KNOWN_PREFIXES = {"claude", "gemini", "google", "codex", "openai", "anthropic", "nvidia", "agy"}
```
In `apply_backend_env`, after the codex block:
```python
    a = routing.backends.get("agy")
    if a:
        if a.cli_bin is not None:
            os.environ.setdefault("AGY_CLI_BIN", a.cli_bin)
        os.environ.setdefault("AGY_CLI_ARGS", " ".join(a.cli_args))
```

- [ ] **Step 4: Run test, verify pass**

Run: `.venv/bin/python -m pytest tests/test_model_routing_backend_env.py tests/test_model_routing_schema.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/model_routing.py tests/test_model_routing_backend_env.py
git commit -m "feat(agy): recognize agy backend/prefix + push AGY_CLI_* env"
```

---

### Task 4: `backends.agy` + the `agy` preset (routing JSON)

**Files:**
- Modify: `src/open_deep_research/data/model_routing.json`
- Test: `tests/test_model_routing_presets.py`

**Interfaces:**
- Consumes: `"agy"` known prefix/backend (Task 3); `agy:` slugs resolved by `to_agy_model` (Task 1).
- Produces: an `agy` preset whose roles resolve to agy slugs with Claude backups.

- [ ] **Step 1: Write failing test**

```python
# tests/test_model_routing_presets.py (add)
def test_agy_preset_resolves(monkeypatch):
    monkeypatch.setenv("MODEL_ROUTING_PRESET", "agy")
    from open_deep_research.model_routing import load_routing, model_chain
    r = load_routing()
    assert "agy" in r.presets
    assert model_chain("researcher", routing=r)[0].startswith("agy:")
    assert model_chain("supervisor", routing=r)[0].startswith("agy:")
    # extract_facts step override present and agy-primary
    assert model_chain("researcher", routing=r, step="extract_facts")[0].startswith("agy:")
```

- [ ] **Step 2: Run test, verify fail**

Run: `.venv/bin/python -m pytest tests/test_model_routing_presets.py -k agy_preset -v`
Expected: FAIL — no `agy` preset.

- [ ] **Step 3: Add `backends.agy` + the `agy` preset to model_routing.json**

In `backends`, add: `"agy": { "cli_bin": "agy", "cli_args": [] }` (NO `--dangerously-skip-permissions` — see security note; opt-in only in a sandbox via `AGY_CLI_ARGS`).
In `presets`, add (do NOT change `active_preset`):
```json
    "agy": {
      "comment": "Antigravity CLI (agy) backend -- the working replacement after the gemini CLI free tier was deprecated. Dispatch (supervisor) routed to Claude-via-agy for reliable tool-calling; gemini-3.x throughput for research/text; claude_agent_sdk backups everywhere (independent of agy auth). extract_facts on gemini-3.5-flash-high (the lean schema makes flash sufficient).",
      "roles": {
        "supervisor": ["agy:claude-opus-4.6", "claude-opus-4-8"],
        "researcher": ["agy:gemini-3.5-flash-high", "claude-opus-4-6"],
        "summarization": ["agy:gemini-3.5-flash-medium", "claude-haiku-4-5"],
        "compression": ["agy:gemini-3.5-flash-medium", "claude-haiku-4-5"],
        "final_report": ["agy:gemini-3.1-pro-high", "claude-opus-4-8"]
      },
      "search": "tavily",
      "step_overrides": { "extract_facts": ["agy:gemini-3.5-flash-high", "claude-haiku-4-5"] }
    }
```

- [ ] **Step 4: Run test + validate the whole file loads**

Run: `.venv/bin/python -c "from open_deep_research.model_routing import load_routing; load_routing(); print('routing OK')"`
Run: `.venv/bin/python -m pytest tests/test_model_routing_presets.py tests/test_model_routing_schema.py -p no:warnings -q`
Expected: PASS (the agy preset validates; every `agy:` model string passes the prefix check).

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/data/model_routing.json
git commit -m "feat(agy): backends.agy + agy preset (Claude dispatch, gemini-3.x throughput)"
```

---

### Task 5: Preflight recognizes the agy backend

**Files:**
- Modify: `src/open_deep_research/preflight.py` (`_probe_uncached`)
- Test: `tests/test_preflight.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_probe_uncached("agy")` returns True when the `agy` binary is present + runnable, False when missing.

- [ ] **Step 1: Write failing test**

```python
# tests/test_preflight.py (add)
def test_probe_agy_backend(monkeypatch):
    from open_deep_research import preflight as pf
    monkeypatch.setattr(pf.shutil, "which", lambda b: "/bin/agy" if b == "agy" else None)
    monkeypatch.setattr(pf.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    assert pf._probe_uncached("agy") is True
    monkeypatch.setattr(pf.shutil, "which", lambda b: None)
    assert pf._probe_uncached("agy") is False
```

- [ ] **Step 2: Run test, verify fail**

Run: `.venv/bin/python -m pytest tests/test_preflight.py -k probe_agy -v`
Expected: FAIL — `_probe_uncached("agy")` falls through to the default (returns True) without probing the binary.

- [ ] **Step 3: Add the agy branch to `_probe_uncached` (preflight.py)**

After the `gemini`/`google` branch:
```python
    if backend == "agy":
        binname = os.environ.get("AGY_CLI_BIN", "agy")
        if shutil.which(binname) is None:
            return False
        try:
            r = subprocess.run([binname, "--version"], capture_output=True, timeout=15)
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False
```

- [ ] **Step 4: Run test, verify pass**

Run: `.venv/bin/python -m pytest tests/test_preflight.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/open_deep_research/preflight.py tests/test_preflight.py
git commit -m "feat(agy): preflight probes the agy backend binary"
```

---

### Task 6: Empirical validation (live agy) + preset tuning

**Files:**
- Possibly modify: `src/open_deep_research/data/model_routing.json` (only if a probe shows a model is unreliable)

- [ ] **Step 1: Probe agy plain generation + lean extraction**

Run a small script: build `AgyCLIChat` via `build_chat_model("agy:gemini-3.5-flash-high")`, ainvoke a plain prompt, and run the real lean-extraction prompt through it + `parse_lean_facts`. Confirm clean output and parsed records.
`MODEL_ROUTING_PRESET=agy ODR_PREFLIGHT=off .venv/bin/python <probe>`
Expected: non-empty, clean, parseable.

- [ ] **Step 2: Probe tool-call envelope reliability for the dispatch role**

Build `build_chat_model("agy:claude-opus-4.6").bind_tools([ConductResearch, ResearchComplete, think_tool])` and ainvoke the real `lead_researcher_prompt` + a research brief, N=4 trials; count valid tool-call envelopes (mirror the dispatch probe from this session).
Expected: a reliable rate (>=3/4). If unreliable, change the `agy` preset `supervisor` to the `claude_agent_sdk` backend (`["claude-opus-4-8", ...]`) and re-probe; record the result.

- [ ] **Step 3: Small end-to-end run on the agy preset**

`MODEL_ROUTING_PRESET=agy ODR_PREFLIGHT=warn .venv/bin/python` an in-process facts-first query (kb-off, low iterations, temp DB) and confirm it produces a real answer through agy.

- [ ] **Step 4: Commit any preset tuning**

If Step 2/3 required a preset change:
```bash
git add src/open_deep_research/data/model_routing.json
git commit -m "fix(agy): tune agy preset from empirical probes"
```
Otherwise record the probe results in the task report; no commit needed.

---

## Self-Review

**Spec coverage:** §1 backend class + addressing → Tasks 1 (`to_agy_model`) + 2 (`AgyCLIChat`, `agy:` routing); §2 invocation (no `-o json`, stdin, skip-permissions, pass-through auth) → Task 2; §3 routing/preset/preflight → Tasks 3 (known prefix/backend + env), 4 (backends.agy + agy preset), 5 (preflight); §4 testing → each task's deterministic tests + Task 6 empirical probes. All spec sections mapped.

**Placeholder scan:** No TBDs. Task 6 is explicitly empirical/observational with a concrete tuning rule (route supervisor to the claude_agent_sdk backend if agy dispatch is unreliable) — not a placeholder.

**Type consistency:** `to_agy_model(slug)->str`, `_AGY_MODELS`, `AgyCLIChat(_CLIJsonChat)` with `_subprocess_env`/`_backend_generate`, `build_chat_model` agy branch, `KNOWN_PREFIXES`/`KNOWN_BACKENDS` += "agy", `apply_backend_env` AGY_CLI_BIN/ARGS, `_probe_uncached("agy")` — names consistent across tasks. `AgyCLIChat.model` is the display name (mapped in `build_chat_model`), matching `_backend_generate`'s `--model self.model`.

**Ordering:** 1 (mapping) → 2 (class + routing, uses mapping) → 3 (known prefix/backend + env, MUST precede the JSON) → 4 (JSON preset, references agy: + backends.agy) → 5 (preflight) → 6 (empirical, tunes the preset).
