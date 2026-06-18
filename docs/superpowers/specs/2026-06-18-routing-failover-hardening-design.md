# Routing Failover Hardening — Design

**Date:** 2026-06-18
**Status:** Approved (design); pending implementation plan
**Builds on:** `2026-06-17-model-failover-chains-design.md`, `2026-06-17-model-routing-as-data-design.md`

## Problem

We switched the active routing preset to `gemini` (every role runs `gemini-2.5-flash`
primary, Claude as cross-backend backup) because the Claude subscription is nearly
exhausted. That exposed weaknesses in the failover path: a backup Claude that is itself
exhausted gets re-tried repeatedly, a logged-out gemini CLI silently dumps the whole
workload onto Claude, and exhaustion is neither remembered across runs nor surfaced
loudly. This work hardens the existing failover machinery so the gemini switch is
trustworthy and cannot quietly drain Claude.

## What already exists (do not rebuild)

- **`failover.py`** — error classification (`hard`/`transient`), a run-scoped
  `AvailabilityTracker` (per-model down-set + recorded failover events), and a
  thread-id-keyed registry that survives LangGraph node boundaries.
- **`claude_agent_chat.py::configurable_claude_model.ainvoke`** — the live failover
  loop: iterates the available chain, marks hard failures down, records + logs each
  failover, raises only when the chain is exhausted.
- **All hot-path roles pass `model_chain`** (supervisor, researcher, compression,
  summarization, final_report, `extract_facts`), so failover is genuinely active.
- **Failovers are already captured** in `config_used["failovers"]` (deep_researcher.py)
  and the tracker is freshly installed per run keyed by `thread_id`.

## Goals (all four confirmed)

1. Protect Claude quota — never silently burn an exhausted Claude.
2. Make failover actually work — close any path where a primary failure isn't covered.
3. Fail loud, not silent — surface backend failure instead of degraded/empty runs.
4. Validate the preset switch — preflight that the active preset's primary backend is usable.

## Design

### Stage 1 — Quota firewall

**G1 · Backend-level mark-down.**
Split the current `_HARD_MARKERS` into two sets:
- *backend-fatal*: `quota`, `insufficient_quota`, `resource_exhausted`, `billing`,
  `unauthorized`, `invalid api key`, `invalid_api_key`, `permission denied`, `401`, `403`.
- *model-fatal*: `model not found`, `model_not_found`, `does not exist`, `404`.

On a **backend-fatal** error, mark the **whole backend** down for the run, not just the
one model string. Backend is derived from the model spec: the `prefix:` before a colon
(`gemini:…`, `codex:…`), else a bare `claude-*`/family name → `claude`. A **model-fatal**
error stays model-specific (today's behaviour). `AvailabilityTracker` gains a
`_down_backends: set[str]`, `mark_backend_down`, and `available_chain` filters out models
whose backend is down. Effect: once Claude returns one quota error, *no* later role retries
any Claude model that run.

**G2 · Cross-run memory.**
A small JSON health file records `{backend: until_epoch}` for backend-fatal exhaustion.
- Path: `ODR_BACKEND_HEALTH_FILE` env, else a default under the OS cache dir
  (`<cache>/odr/backend_health.json`).
- TTL: `ODR_BACKEND_HEALTH_TTL` seconds, default `900` (15 min). `0`/unset-to-disable via
  `ODR_BACKEND_HEALTH=off`.
- On backend-fatal mark-down, write `{backend: now + ttl}`. On run start, load the file;
  any backend whose `until_epoch` is in the future is treated as down from the chain head,
  so new runs skip the dead backend instead of re-paying its failed first call.
- Reset = delete the file (or let the TTL lapse). Reads/writes are best-effort and never
  fail a run (corrupt/locked file → ignore + log).

**G3 · Run-start preflight.**
At run start (`new_run_tracker`, deep_researcher.py:1481) probe each **primary** backend of
the active preset once per process (memoized). For `gemini`, a cheap CLI auth check
(e.g. a non-interactive `whoami`/version-with-auth probe — exact command confirmed during
implementation). Behaviour via `ODR_PREFLIGHT`:
- `warn` (**default**): on an unusable primary backend, log a prominent warning and
  proactively `mark_backend_down` so the run skips straight to the backup — avoids N failed
  primary calls per run.
- `fail`: raise a clear, actionable error before any work ("active preset 'gemini' primary
  backend gemini is not logged in; run `gemini auth login` or set MODEL_ROUTING_PRESET").
- `off`: skip the probe.

### Stage 2 — Loud & complete

**G4 · Gemini auth-string coverage.**
Add the real logged-out gemini CLI markers to the *backend-fatal* set so a logged-out CLI
fails **over** rather than into transient retry-burn. Candidate substrings: `not logged in`,
`please authenticate`, `reauthenticate`, `no credentials`, `not authenticated`. Exact
strings verified against the installed CLI during implementation; add a defensive superset.

**G5 · Empty-notes guard.**
Where the supervisor gathers researcher output: if every researcher's chain exhausted and
the run has **zero usable notes**, raise a clear run error carrying the failover summary
(stage, from→to, last reason) instead of synthesizing a report from nothing. This converts
the "worst outcome" (an empty-notes run silently written as a report) into a loud, diagnosable
failure.

**G6 · sync/stream failover gap.**
`invoke()` (sync) has no failover and `astream()` does not fail over mid-stream. Confirm no
hot role uses these as its primary path (the graph uses `ainvoke`). Then:
- Add a guard: if a **multi-element** chain reaches the no-failover sync `invoke()` path,
  raise rather than silently running only the head.
- Document the `astream` mid-stream limitation in-code (already partly noted).

## Testing (TDD — test first per item)

- **G1**: backend-fatal error on `claude-opus-4-6` marks `claude` down → a later
  `claude-opus-4-8` lookup is skipped; a model-fatal 404 does **not** kill the backend.
- **G2**: a backend-fatal mark-down writes the health file; a fresh tracker started within
  TTL treats the backend as down; past-TTL clears it; `off` disables; corrupt file is ignored.
- **G3**: `fail` raises on an unusable primary; `warn` marks the backend down without raising;
  `off` does nothing; probe is memoized (called once).
- **G4**: each new gemini auth string classifies `hard`/backend-fatal.
- **G5**: a fan-out where all researchers exhaust raises the empty-notes error with summary;
  a partial-success run still proceeds.
- **G6**: a multi-element chain through sync `invoke()` raises the guard.

## Files touched

- `src/open_deep_research/failover.py` — G1, G2, G4 (split markers, backend mark-down,
  health-file load/save, backend derivation).
- `src/open_deep_research/preflight.py` (new, small) — G3 probe + policy.
- `src/open_deep_research/deep_researcher.py` — G3 call at run start, G5 empty-notes guard.
- `src/open_deep_research/claude_agent_chat.py` — G6 sync-path guard; consume backend
  mark-down in `available_chain` (already routed through the tracker).
- Tests alongside existing failover tests.

## Non-goals

- No change to preset definitions or the routing-as-data schema.
- No retry-policy/backoff redesign (transient handling stays as-is).
- No new backend; no UI surface beyond logs + the existing `config_used["failovers"]`.

## Risks / open questions

- Exact gemini CLI auth-probe command and logged-out error text — resolved during
  implementation against the installed CLI (fallback: defensive superset of markers).
- Health-file location on Windows-host vs WSL — use a stable per-user cache dir; the server
  runs on the Windows host, so the default must resolve there.
