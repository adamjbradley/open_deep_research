# Design — Model Routing as Data

- **Date:** 2026-06-17
- **Layer:** Feature Spec / Design
- **Status:** Draft — approved in brainstorming; ready for plan.
- **Builds on:** the per-role model config in `configuration.py` (`*_model`, `search_api`, read via `from_runnable_config`), the CLI backends in `claude_agent_chat.py`, and the *profiles-as-data* pattern (editable, validated, dynamically-loaded config). This is the same pattern applied to **model/search routing**.

## Context & problem

The graph chooses a model at many call sites, all resolving to one of ~6 **roles** — `supervisor_model`, `researcher_model`, `summarization_model`, `compression_model`, `final_report_model`, `facts_answer_polish_model` — plus a global `search_api`. These are `Configuration` fields, already overridable per-role via `.env` (`from_runnable_config` reads `SUPERVISOR_MODEL` etc.).

Three gaps make routing painful in practice (proven this session while making gemini work):
1. **Backend settings are scattered code defaults.** Getting the standard gemini CLI working needed four non-obvious settings (`GEMINI_CLI_BIN=gemini`, drop `--dangerously-skip-permissions`, model `gemini-2.5-flash` not `2.0-flash`, `GEMINI_CLI_TRUST_WORKSPACE=true`) buried in `claude_agent_chat.py`. Codex needs a restricted sandbox. None of this is configurable in one place.
2. **No presets.** Switching the whole pipeline backend means setting ~6 env vars by hand. We learned "all-gemini works, all-codex breaks, claude works" — a one-line preset toggle would make that trivial.
3. **No finer-than-role control in data.** E.g. "gemini everywhere except `claude:sonnet` for fact extraction" can't be expressed without code.

**Outcome:** a single, validated, dynamically-read **`model_routing.json`** that holds named **presets** (whole-pipeline backend bundles), per-role models, optional **per-step overrides**, and per-**backend settings** — resolved beneath the existing config so the graph and backend code are untouched and `.env` still works as a quick override.

## Locked decisions (from brainstorming)
1. **Scope = all of:** capture per-backend settings, named presets, one structured file, and finer per-step granularity.
2. **Resolution = layered with fallback:** `explicit env > file step_override > file role (active preset) > code default`.
3. **Format = JSON** (`model_routing.json`). (YAML noted as a consistency option with profiles/registries; JSON chosen per owner request.)
4. **Integration = a resolver consulted by `Configuration.from_runnable_config`** (model roles) + `os.environ.setdefault` population for backend settings — graph and `claude_agent_chat.py` call sites unchanged.

## File format

`model_routing.json` (read per run; precedence for the file path: `MODEL_ROUTING_FILE` env → `./model_routing.json` → a bundled default in the package):

```json
{
  "version": "1",
  "active_preset": "gemini",
  "backends": {
    "gemini": { "cli_bin": "gemini", "cli_args": [], "trust_workspace": true,
                "model_aliases": { "flash": "gemini-2.5-flash" } },
    "codex":  { "cli_bin": "codex", "sandbox": "read-only" },
    "claude": { "subscription": true }
  },
  "presets": {
    "gemini": {
      "roles": { "supervisor": "gemini:gemini-2.5-flash", "researcher": "gemini:gemini-2.5-flash",
                 "summarization": "gemini:gemini-2.5-flash", "compression": "gemini:gemini-2.5-flash",
                 "final_report": "gemini:gemini-2.5-flash", "facts_answer_polish": "gemini:gemini-2.5-flash" },
      "search": "tavily",
      "step_overrides": { "extract_facts": "claude:sonnet" }
    },
    "claude": { "roles": { "supervisor": "claude:sonnet", "researcher": "claude:sonnet",
                           "summarization": "claude:sonnet", "compression": "claude:sonnet",
                           "final_report": "claude:sonnet", "facts_answer_polish": "claude:sonnet" },
                "search": "tavily" }
  }
}
```

The **bundled default** ships with `active_preset: "gemini"` and the exact models the code defaults currently use, so behavior is identical until the file is edited.

## Resolution order (highest wins)
1. **Explicit env var** for the role (`SUPERVISOR_MODEL`, `RESEARCHER_MODEL`, …, `SEARCH_API`) — preserves the current quick-override mechanism.
2. **File `step_overrides[step]`** — only for steps that ask for per-step resolution (see Plumbing).
3. **File `presets[active].roles[role]`** (active preset = `MODEL_ROUTING_PRESET` env → file `active_preset`).
4. **Code default** in `configuration.py` — final safety net if the file is absent/partial.

`active_preset` itself: `MODEL_ROUTING_PRESET` env overrides the file's `active_preset`.

## Components

### 1. `model_routing.py` (new)
- `load_routing() -> RoutingConfig` — resolve path, parse JSON, **validate** (meta-schema), return a typed object. Cheap; may memoize per `(path, mtime)` so per-run reads pick up edits without re-parsing unchanged files.
- `resolve_model(role: str, *, config, step: str | None = None) -> str` — implements the order above.
- `resolve_search(*, config) -> str`.
- `active_backend_settings(routing) -> dict[str, dict]` — the `backends` block for the active preset's backends.
- `apply_backend_env(routing) -> None` — `os.environ.setdefault(...)` for `GEMINI_CLI_BIN`, `GEMINI_CLI_ARGS`, `GEMINI_CLI_TRUST_WORKSPACE`, codex sandbox flag, etc., from `backends`. Called once when routing first loads. `setdefault` so explicit env always wins.

### 2. Meta-schema (`model_routing_schema.py` or within the module)
Pydantic model validating: `version`; `active_preset` exists in `presets`; each role value is a non-empty string whose backend prefix (`claude:`/`gemini:`/`codex:`/bare) is known; `backends` keys are known backends; `step_overrides` keys are in a known-steps allowlist; `search` is a valid `SearchAPI`. Errors name file + field + problem. Reused by a `validate` surface.

### 3. `Configuration.from_runnable_config` integration
For each `*_model` field and `search_api`: value = `resolve_model(role, config=...)` / `resolve_search(...)` instead of the current `os.environ.get(FIELD.upper(), configurable.get(field))`. The resolver itself still checks the env var first (step 1), so the existing `.env` behavior is preserved; the file fills the gap between env and code default. On first resolution, `apply_backend_env(routing)` runs so backend settings reach `claude_agent_chat.py`.

### 4. Per-step plumbing
Add `Configuration.model_for(step: str, fallback_role: str) -> str` → `resolve_model(fallback_role, config=self, step=step)`. Adopt incrementally at the few sites that want finer control — primarily `extract_facts`/`_make_fact_model_call` (the canonical "sonnet for extraction" case). All other call sites keep `configurable.<role>_model` unchanged.

### 5. CLI / validation surface
`dossier validate` also validates `model_routing.json` if present (or a dedicated `model-routing validate`). A `model-routing show [--preset X]` that prints the fully-resolved model per role/step is a nice-to-have for inspectability (deferred if it bloats scope).

## Data flow
```
graph node needs a model
  -> Configuration.from_runnable_config(config)        # per run
       -> for each role: resolve_model(role) :  env > file.step_override > file.preset.role > code default
       -> apply_backend_env(routing): os.environ.setdefault(GEMINI_CLI_BIN=..., TRUST=..., codex sandbox=...)
  -> node uses configurable.<role>_model  (unchanged)  # or model_for(step, role) for per-step
  -> claude_agent_chat backend reads getenv(...) -> values from the file (via setdefault)
edit model_routing.json (or MODEL_ROUTING_PRESET=claude) -> next run picks it up, no restart
```

## User stories (acceptance criteria)
- **US-1 presets:** `MODEL_ROUTING_PRESET=claude` (or `active_preset` in the file) flips every role to claude. *AC:* resolved models for all roles change with one setting; switching back to `gemini` restores them.
- **US-2 per-step override:** the `gemini` preset routes `extract_facts` to `claude:sonnet` while other roles stay gemini. *AC:* `model_for("extract_facts","researcher")` returns `claude:sonnet`; `researcher_model` returns gemini.
- **US-3 backend settings from data:** the gemini backend's `cli_bin`/`trust_workspace` come from the file and reach `claude_agent_chat.py`. *AC:* with no `GEMINI_*` env set, a routing file setting `cli_bin: gemini`, `trust_workspace: true` makes those appear in the subprocess env.
- **US-4 env still wins:** `SUPERVISOR_MODEL=claude:sonnet` overrides the file's preset for that role only. *AC:* env beats file; other roles unaffected.
- **US-5 absent file:** with no `model_routing.json`, the code defaults apply unchanged (current behavior). *AC:* deleting the file leaves the suite green and the graph working.
- **US-6 validation:** a malformed routing file (unknown preset/backend/step, bad model prefix) fails `dossier validate` with a located error. *AC:* seeded bad file → non-zero, clear message; the real file passes.
- **US-7 dynamic:** editing the file changes the next run without a restart. *AC:* change `active_preset`, re-resolve, models differ — no process restart.

## Required coverage
- **Safety & harm (epistemic):** Routing decides *which model produces facts*; a bad route (e.g. an agentic CLI) yields wrong/empty facts. Mitigated by: validation-on-load, presets that bundle known-good settings, env-override escape hatch, and the code-default safety net. The `codex` backend's `sandbox: read-only` default encodes the lesson that codex must not run repo commands.
- **Inclusion:** N/A to end users; improves operator accessibility (one readable file vs scattered env/code).
- **Legal & compliance:** No PII; the file is config. Backend `subscription`/key-emptiness settings keep billing on logins not paid API — document that emptying keys belongs in `.env`, not this file (secrets stay out of the routing file).
- **Risk & exploitation:** The file is a repo artifact under code review, never user-supplied at runtime. No secrets in it (only model ids + CLI flags). `apply_backend_env` uses `setdefault` so it can't silently override an operator's explicit env.
- **Erosion over time:** Routing drifts from reality (e.g. a model id 404s, as `gemini-2.0-flash` did). Mitigated by validation + the `version` field + presets as the single edit point; `model-routing show` (if built) makes the effective routing inspectable.
- **Economic viability:** Directly enables cost control — route bulk steps to cheap/free-tier backends (gemini), reserve premium models for extraction. Presets make per-platform token spreading a one-liner.
- **Unknown unknowns:** surfaced in `*.feedback` review — precedence edge cases (empty-string env vs unset), per-run read performance under a 215-country batch, and whether backend settings belong in env-population vs being read directly by `claude_agent_chat.py`.

## Critical files (seams)
- `open_deep_research/model_routing.py` *(new)* — loader, resolver, `apply_backend_env`.
- `open_deep_research/model_routing_schema.py` *(new, or inline)* — Pydantic meta-schema.
- `open_deep_research/data/model_routing.json` *(new)* — bundled default (active preset = gemini, current code-default models). Packaged like `factbase/data/*`.
- `open_deep_research/configuration.py` — `from_runnable_config` consults the resolver; add `model_for(step, fallback_role)`; keep current field defaults as the final fallback.
- `open_deep_research/deep_researcher.py` — adopt `model_for("extract_facts", ...)` at the extraction seam (only finer-grained site initially).
- `open_deep_research/claude_agent_chat.py` — unchanged (reads env, now populated from the file); optionally read backend settings directly in a fast-follow.
- `factbase/dossier.py` — validate the routing file in `validate`.
- `.env.example` — document `MODEL_ROUTING_FILE`, `MODEL_ROUTING_PRESET`, and that per-role env vars still override the file.
- `tests/` — `test_model_routing_resolve.py`, `test_model_routing_schema.py`, `test_model_routing_backend_env.py`, plus a wiring test that a node uses the resolved model.

## Verification
1. `uv run pytest` — resolver precedence (env > step > role > preset > default); validation accept/reject; `apply_backend_env` populates gemini bin/trust; preset switch changes resolved models; absent-file falls back to code defaults.
2. `uv run dossier validate` — bundled `model_routing.json` OK; a seeded bad file fails with a located error.
3. Manual: `MODEL_ROUTING_PRESET=claude` flips a real run's models (inspect via logging or `model-routing show`); editing the file changes the next run with no restart.
4. Live smoke (opt-in): a 1-country CBDC run using the bundled gemini preset produces facts (parity with the verified env-based gemini run).

## Out of scope (deferred)
- Per-step overrides for **every** node (only the extraction seam adopts `model_for` initially; others stay role-level).
- A UI/`model-routing show` rich inspector (basic validate first).
- Secrets/keys in the routing file (stay in `.env`).
- YAML format (JSON chosen; revisitable).
- Routing the **search backend** per step (search stays preset-level with one `search` value; per-step search is a fast-follow if needed).
