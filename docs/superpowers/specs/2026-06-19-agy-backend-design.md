# agy (Antigravity CLI) Backend — Design

**Date:** 2026-06-19
**Status:** Approved (design); pending implementation plan
**Context:** Google deprecated the `gemini` CLI free tier mid-session (`IneligibleTierError`
→ "migrate to the Antigravity suite"), so every gemini call now fails. The `agy` CLI (the
Antigravity replacement) is installed (`~/.local/bin/agy`, v1.0.8), authenticates, and works.
This adds a unified `agy:` backend so any role can route to agy's models. The codebase already
anticipated agy (`GeminiCLIChat` docstring, `GEMINI_CLI_BIN`, agy-flag comments).

## Problem & findings (verified during exploration)

- gemini CLI: dead (`IneligibleTierError`, exit 1 on every call).
- agy: works — real call returns output, exit 0, no auth error.
- agy exposes a richer, newer lineup through ONE CLI: **Gemini 3.5 Flash (Low/Medium/High)**,
  **Gemini 3.1 Pro (Low/High)**, **Claude Opus/Sonnet 4.6 (Thinking)**, **GPT-OSS 120B**.
- agy reads the prompt from **stdin** (the mechanism `GeminiCLIChat` already uses).
- agy **rejects `-o json`** (`flags provided but not defined: -o`, exit 2). Its plain output is
  already clean (no `update_topic`/control-token artifacts), so `-o json` is unnecessary.
- agy's `--model` requires the **exact display-name string** (e.g. `"Gemini 3.1 Pro (High)"`);
  any unrecognized id **silently defaults to Gemini 3.5 Flash** — a real footgun.
- The real lean-extraction prompt through agy produced clean JSON; `parse_lean_facts` got 6
  records with the flat qualifier token — so agy + the lean schema work together.

## Goals

Unified `agy:` backend: any role can route to any agy model (Gemini 3.x / Claude 4.6 / GPT-OSS)
via stable, tier-explicit slugs, with the silent-default footgun closed, reusing the existing
CLI-backend machinery and the session's tool-dispatch-reliability lessons.

## Section 1 — Backend class & model addressing

- **`agy:` prefix** added to `KNOWN_PREFIXES` and `KNOWN_BACKENDS` (`model_routing.py`).
  `build_chat_model`/`parse_backend` route `agy:` → a new `AgyCLIChat`.
- **`AgyCLIChat(_CLIJsonChat)`** — a dedicated class alongside `GeminiCLIChat`/`CodexCLIChat`,
  reusing the base's JSON-envelope tool-call/structured coercion (agy, like the gemini CLI,
  has no native schema flag). Overrides `_backend_generate` for the agy command.
- **`to_agy_model(slug) -> str`** — the source-of-truth mapping from stable, tier-explicit
  slugs to agy's exact display names:
  ```python
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
  ```
  It strips a leading `agy:` prefix, then looks up the slug; an **unknown slug raises
  `ValueError`** (never let agy silently default to Gemini 3.5 Flash). Slugs seeded from
  `agy models`; the table is the only place to extend.

## Section 2 — CLI invocation

In `AgyCLIChat._backend_generate`:
```python
bin_ = os.getenv("AGY_CLI_BIN", "agy")
extra = os.getenv("AGY_CLI_ARGS", "--dangerously-skip-permissions").split()
cmd = [bin_, "--model", to_agy_model(self.model), *extra]   # NO -o json
raw = await self._invoke(cmd, stdin=full)                    # prompt via stdin
# strip a trailing "### Summary" section if present (reuse existing cleanup)
```
- **No `-o json`** (agy rejects it; output is already clean). Because `AgyCLIChat` is a separate
  class, `GeminiCLIChat` keeps its `-o json` untouched.
- **`--dangerously-skip-permissions`** so agy auto-approves tool prompts non-interactively;
  configurable via `AGY_CLI_ARGS` / the backend `cli_args`.
- **Auth/env:** `AgyCLIChat._subprocess_env` passes the environment through WITHOUT blanking
  credentials (agy uses the Antigravity login store, not `GEMINI_API_KEY`). Minimal — verified
  working with the plain env.
- **Tool-call / structured output:** reuse the `_CLIJsonChat` base (append tool catalog +
  JSON-envelope schema to the prompt, parse the returned JSON into `AIMessage.tool_calls`).
- **Infra reuse:** existing `_invoke`/`_offload_subprocess`/drain-timeout/concurrency machinery,
  unchanged — agy is just another CLI subprocess.

## Section 3 — Routing, preset & preflight

- `apply_backend_env` extended to push the agy backend's `cli_bin`/`cli_args` into
  `AGY_CLI_BIN`/`AGY_CLI_ARGS` (mirroring gemini/codex).
- `backends.agy`: `{ "cli_bin": "agy", "cli_args": ["--dangerously-skip-permissions"] }`.
- A new **`agy` preset** (reliable models for tool-dispatch, gemini-throughput for text, Claude
  SDK backups everywhere — independent of agy auth):
  ```
  supervisor    : ["agy:claude-opus-4.6",        "claude-opus-4-8"]
  researcher    : ["agy:gemini-3.5-flash-high",  "claude-opus-4-6"]
  summarization : ["agy:gemini-3.5-flash-medium","claude-haiku-4-5"]
  compression   : ["agy:gemini-3.5-flash-medium","claude-haiku-4-5"]
  final_report  : ["agy:gemini-3.1-pro-high",    "claude-opus-4-8"]
  step_overrides.extract_facts: ["agy:gemini-3.5-flash-high","claude-haiku-4-5"]
  ```
  (Lean schema means flash should handle extraction; pro isn't forced.)
- `build_chat_model`/`parse_backend`: `agy:` → `AgyCLIChat`.
- `preflight.py`: recognize the `agy` backend — `shutil.which("agy")` + a cheap
  `agy --version`/`agy models` probe, so a missing/broken agy is caught up front like gemini.

## Section 4 — Testing

**Deterministic (gate the merge):**
- `to_agy_model`: known slug → exact display name; **unknown slug raises `ValueError`**.
- `AgyCLIChat` cmd: `[agy, "--model", "<display>", "--dangerously-skip-permissions"]`, no
  `-o json`, prompt via stdin (mock `_invoke`).
- `agy:` prefix → `AgyCLIChat` with the mapped model.
- routing: `"agy"` in `KNOWN_PREFIXES`/`KNOWN_BACKENDS`; the `agy` preset + `backends.agy`
  load/validate; `apply_backend_env` pushes `AGY_CLI_BIN`/`AGY_CLI_ARGS`.
- preflight recognizes the agy backend (probe mocked).

**Empirical probes (live agy, gated — like this session's gemini/codex probes):**
- plain generation clean on agy;
- **tool-call envelope reliability** for the dispatch role (`agy:claude-opus-4.6` → valid
  `ConductResearch` envelope N/N) — the key risk; adjust the preset if a model is unreliable;
- lean extraction on `agy:gemini-3.5-flash-high` parses records;
- a small end-to-end run on the `agy` preset.

## Files touched (anticipated)

- `claude_agent_chat.py` — `AgyCLIChat` class, `to_agy_model`, `agy:` in `build_chat_model`/`parse_backend`.
- `model_routing.py` — `agy` in `KNOWN_PREFIXES`/`KNOWN_BACKENDS`; `apply_backend_env` agy push.
- `data/model_routing.json` — `backends.agy` + the `agy` preset.
- `preflight.py` — agy backend probe.
- Tests alongside existing backend/routing/preflight tests.

## Non-goals

- Not removing/altering the existing `gemini`/`codex`/`claude` backends (agy is additive).
- Not auto-switching `active_preset` to `agy` (left to the operator; `claude` is the current
  stopgap).
- Not exhaustively mapping every agy model/tier — seed the common ones; the table extends easily.

## Risks / open questions

- **Tool-call envelope reliability on agy** is the main unknown (dispatch is envelope-sensitive,
  as gemini taught us). Mitigated by routing dispatch to a Claude model and Claude SDK backups;
  confirm with the empirical probe and tune the preset.
- **agy model-id drift:** agy's display names could change across versions, silently re-routing
  to the Gemini 3.5 Flash default. Mitigated by the unknown-slug-raise guard on OUR slugs, but a
  changed agy display name would still mis-route — a periodic `agy models` check is prudent.
- **Supervisor backend choice:** dispatch is routed to `agy:claude-opus-4.6` (Claude via agy).
  If the agy Claude tier is unreliable or undesirable, swap to the `claude_agent_sdk` backend
  (`claude-opus-4-8`) — a one-line preset change.
- **Antigravity auth longevity:** agy depends on the Antigravity login remaining valid; the
  preflight surfaces a logged-out agy, and the Claude SDK backups keep runs working if agy lapses.
