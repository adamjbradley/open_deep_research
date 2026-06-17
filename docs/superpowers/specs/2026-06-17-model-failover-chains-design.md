# Design — Model Failover Chains + Reactive Failover

- **Date:** 2026-06-17
- **Layer:** Feature Spec / Design
- **Status:** Draft — pending user approval, then `*.feedback` multi-agent review
- **Builds on:** `2026-06-17-model-routing-as-data-design.md` (the shipped `model_routing.json` "routing as data" seam: presets, per-role models, per-step overrides, per-backend settings; resolution `env > configurable > step_override > role > code default`).

## Context & problem

Routing today resolves every stage to exactly **one** model string (`model_routing.py:resolve_model`, a pure config-precedence chain). If that model is unavailable mid-run — Gemini daily quota exhausted, a `404` model id, an auth failure — the run has no alternative. What exists is **retry on the same model**: `_run_with_retry` (`claude_agent_chat.py`) retries transient CLI/SDK failures (its marker list already includes `"rate limit"` / `" 429"`), and `.with_retry(stop_after_attempt=…)` retries structured-output calls. Both re-hit the *same* model; neither switches. `is_token_limit_exceeded(...)` (`deep_researcher.py:651/919/1018`) is context-window handling, not quota. So an out-of-quota stage retries a few times and then fails hard.

A long, multi-stage research run is exactly where a backend exhausts quota partway through (it burns tokens *after* any start-of-run check would have passed). The fix is **per-stage backup models** plus **reactive failover** at the point of call.

## Locked decisions (from brainstorming)

1. **Failover reach = cross-backend.** A backup may be a different provider (e.g. `gemini:gemini-2.5-pro` → `claude-opus-4-8`), so the run survives an entire backend going down. Because this can route a stage onto a pricier provider, every failover is logged + recorded (decision 4).
2. **Reactive only, no preflight.** Catch the unavailable/quota error at the actual call and fail over to the next model in the chain. One mechanism covers "dead at start" and "died mid-run". No per-run ping latency.
3. **Sticky for the run.** A model that hits a *hard* (non-transient) error is marked down for the rest of that run and skipped by all later calls; the down-set is run-scoped and resets next run. Transient failures never mark a model down.
4. **Log every failover; no hard cap.** Each failover emits a `WARN` line and is appended to the run's stored metadata. Resilience wins over a budget ceiling in v1 (ceiling is deferred, see Out of scope).
5. **Failover logic lives inside `configurable_claude_model`** (Approach A), the single chokepoint every role already flows through (`configurable_claude_model._materialize()`), so all stages are covered with no graph/call-site rewrites.

## Architecture

Two units:

- **`failover.py` (new)** — pure policy, no I/O. Owns:
  - `classify_error(exc) -> "transient" | "hard"` — the error classifier (§ Error classification).
  - `AvailabilityTracker` — a run-scoped record of models marked **down** (hard-failed). `mark_down(model)`, `is_down(model)`, and `next_available(chain) -> model | None` (first chain entry not marked down). Keyed per run so state resets each run.
  - `FailoverRecord` — `{stage, from_model, to_model, reason}` for logging + persistence.
- **`configurable_claude_model` (extended, `claude_agent_chat.py`)** — consumes the policy. On invoke it resolves the *chain* (not a single model), skips any entry the tracker has marked down, materialises the head, and runs it. On failure it asks `classify_error`: **transient** → existing `_run_with_retry` behaviour against the *same* model; **hard** → `mark_down(current)`, emit a `FailoverRecord`, advance to `next_available(chain)`, materialise + replay the recorded queue (`bind_tools` / `with_structured_output` / `with_retry`) and retry. If the chain is exhausted, raise the last error.

`model_routing.py` and the graph nodes are otherwise unchanged. The graph keeps calling `configurable_model.with_config({...}).ainvoke(...)`; the only difference is the config dict now also carries the chain.

## Data model — chains as data (back-compatible)

A preset role value becomes **either a string (today) or a list (primary first)**:

```jsonc
"roles": {
  "supervisor":   ["gemini:gemini-2.5-pro", "claude-opus-4-8"],  // primary, then cross-backend backup
  "researcher":   ["gemini:gemini-2.5-flash", "claude-haiku-4-5"],
  "summarization": "gemini:gemini-2.5-flash"                      // bare string = chain of length 1 (valid)
}
```

- **Validation (`Preset._check`)** accepts `str | list[str]`; a list must be non-empty and every entry passes the existing `_check_model_string`. `step_overrides` accept the same shape.
- **Resolution.** Add `model_chain(role, *, step, env_value, configurable_value, code_default) -> list[str]` to `model_routing.py`. It applies the *same precedence* as `resolve_model` (env > configurable > step_override > role > code_default) and normalises whatever wins to a list. An explicit env/configurable override (a single string) yields a one-element chain — an override deliberately opts out of failover, which is the right least-surprise behaviour.
- **Back-compat.** `resolve_model` stays and returns `model_chain(...)[0]` (the head), so `Configuration.supervisor_model` and every existing caller/test keep working unchanged.

### Threading the chain to the model

Each `*_model_config` dict in `deep_researcher.py` already carries `"model"`. It gains a sibling `"model_chain"` (the resolved list) and a `"stage"` label (for log/record). `configurable_claude_model.with_config` captures both keys (mirroring how it already captures `"model"`/`"max_tokens"`). `_materialize` uses the chain head (minus down models); the invoke wrapper uses the full chain + stage for failover. Call sites change by one line each (add the chain to the config dict they already build) — no structural change.

## Error classification — the crux

Two buckets:

- **Transient** → retry the **same** model first (this is the current `_run_with_retry` path): throttle `429` ("rate limit"), overload / `503`, request timeouts, hung-subprocess timeouts, generic SDK/CLI blips.
- **Hard-unavailable** → fail over **immediately** (don't burn retries on a model that won't come back this run): quota / credits exhausted (`"quota"`, `"insufficient_quota"`, `"exhausted"`, `"billing"`), model-id `404` / "model not found", auth failure (`401` / "unauthorized" / "invalid api key").

**The ambiguous case — bare `429`.** A `429` can mean "slow down" (transient) or "daily quota gone" (hard). Rule: if the message carries a quota/exhaustion marker → **hard**; a bare rate-limit `429` → **transient**. For a genuinely ambiguous error that neither matches cleanly, default to **transient-first, then fail over**: `_run_with_retry` retries it a bounded couple of times; if it still fails, the invoke wrapper escalates it to **hard** and switches. So ambiguity costs a little retry latency but still ends up on the backup rather than crashing the run.

`classify_error` is a pure, table-driven function (substring matchers + exception-type checks) so the matchers are unit-testable in isolation and easy to extend per backend.

## Data flow

```
stage call ─▶ configurable_model.with_config({model, model_chain, stage, ...}).ainvoke(...)
                 │
                 ├─ chain = [m for m in model_chain if not tracker.is_down(m)]
                 ├─ materialise chain[0], replay queue, invoke
                 │     ├─ success ─▶ return
                 │     └─ error ─▶ classify_error(e)
                 │            ├─ transient ─▶ _run_with_retry (SAME model)  ─▶ success | escalate-to-hard
                 │            └─ hard ─▶ tracker.mark_down(chain[0])
                 │                        record FailoverRecord(stage, from, to, reason)
                 │                        WARN log; advance to next available; materialise + retry
                 └─ chain exhausted ─▶ raise last error
run finalize ─▶ persist run.failovers = [FailoverRecord, ...]  (existing SQLite path)
```

## User stories (acceptance criteria)

- **US-1 mid-run quota death:** primary exhausts quota on call N; the stage continues on its backup. *AC:* fake backend raises quota-exhausted on call 1 → stage result comes from the backup model; no exception escapes.
- **US-2 sticky:** once a model hard-fails, later calls in the same run skip it. *AC:* after US-1, call 2 of the same stage goes straight to the backup (primary not re-invoked); the tracker reports the primary down. A *different* stage/model is still attempted normally.
- **US-3 transient ≠ down:** a throttle/timeout retries the same model and never marks it down. *AC:* a backend raising a bare-`429` then succeeding resolves on the **primary**; tracker shows nothing down; no FailoverRecord emitted.
- **US-4 cross-backend:** a Gemini primary fails over to a Claude backup. *AC:* chain `["gemini:gemini-2.5-pro", "claude-opus-4-8"]` with the Gemini call hard-failing → the Claude backup is invoked (verified by which backend `build_chat_model` constructed).
- **US-5 visibility:** every failover is logged and persisted. *AC:* a `WARN failover[<stage>]: <from> unavailable (<reason>) → <to>` line is emitted and the finished run's stored metadata contains a matching `failovers` record.
- **US-6 back-compat:** a bare-string role still works and a single-model chain has no failover behaviour. *AC:* all existing `model_routing` / configuration tests pass unchanged; a string role resolves to a one-element chain that raises (not silently swallows) when its only model hard-fails.
- **US-7 exhausted chain:** when every model in the chain is down, the run fails loudly. *AC:* a two-model chain where both hard-fail raises the last error (no silent empty result).

## Required coverage

- **Safety & harm:** No self-harm/crisis surface. The risk here is **silent degradation** — a run quietly completing on a weaker/pricier backup and the user not knowing. Mitigated by decision 4: every failover is logged + recorded on the run, so degraded runs are always visible after the fact. US-7 ensures a fully-exhausted chain fails loudly rather than returning empty notes that persist as a "completed" dossier.
- **Inclusion:** N/A (infrastructure-level reliability; no user-facing content change).
- **Legal & compliance:** Failover can move data between providers (Gemini → Claude). That routing is now **auditable** — the run record names which backend actually served each stage, giving a provenance trail for where data was processed. No new PII surface; model strings only.
- **Risk & exploitation:** (a) **Cost surprise** — cross-backend can route to a pricier provider; logging makes it visible, and a hard cap is a named deferred fast-follow if it bites. (b) **Masking misconfiguration** — a wrong/typo'd primary would silently always fail over; mitigated because the failover is logged every time and `classify_error` treats `404`/auth as hard (so a broken primary is loud in the logs, not invisible). (c) **Retry amplification** — escalate-to-hard on ambiguous errors bounds wasted retries on a dead backend (vs. retrying forever).
- **Erosion over time:** Failure mode = the down-set or classifier drifting out of sync with real backend error strings. Addressed by the table-driven, unit-tested `classify_error` (easy to extend per backend) and run-scoped tracker state (no cross-run leakage to debug).
- **Economic viability:** Turns a hard run failure (wasted entire run + manual restart) into a graceful continuation on a backup — strictly improves the cost of a quota event. Added cost: a few extra tokens on the ambiguous-retry path; negligible.
- **Unknown unknowns:** Surface in the adversarial `*.feedback` round — pressure-test (a) the `429` transient-vs-hard split against real CLI error strings from each backend, (b) failover behaviour under the **parallel** researcher fan-out (shared tracker across concurrent `asyncio` tasks — see Open questions), (c) interaction with the existing `_run_with_retry` so the two retry layers don't multiply.

## Critical files (seams — minimal)

- `src/open_deep_research/failover.py` *(new)* — `classify_error`, `AvailabilityTracker`, `FailoverRecord`. Pure, no I/O.
- `src/open_deep_research/model_routing.py` — accept `str | list[str]` for roles/step_overrides in `Preset._check`; add `model_chain(...)`; keep `resolve_model` as `model_chain(...)[0]`.
- `src/open_deep_research/claude_agent_chat.py` — `configurable_claude_model` captures `model_chain`/`stage` in `with_config`; failover loop in the invoke path; reuse `_run_with_retry` for the transient branch.
- `src/open_deep_research/deep_researcher.py` — each `*_model_config` dict adds `model_chain` (from `configurable.model_chain(role)`) + `stage`; thread the run-scoped tracker (one per run) into the config. No node-structure change.
- `src/open_deep_research/configuration.py` — add a `model_chain(role, step)` accessor mirroring the existing `model_for`, resolving through `model_routing.model_chain`.
- `src/open_deep_research/storage.py` — persist `failovers` on the run record (extend the existing run metadata write).
- `tests/` — `test_failover.py` (classifier table; tracker stickiness; chain resolution string/list/back-compat) and an integration test (fake backend hard-fails call 1 → lands on backup; transient retries same; record persisted).

## Verification

1. `uv run pytest` — all existing `model_routing` / configuration / graph tests green (back-compat preserved).
2. Classifier table test: each representative error string/type → expected `transient`/`hard` bucket, including the bare-`429` vs quota-`429` split and the ambiguous→escalate path.
3. Tracker test: `mark_down`/`is_down`/`next_available`; run-scoped reset; a transient failure leaves the model available.
4. Chain resolver test: string form, list form, env/configurable override → one-element chain, precedence order matches `resolve_model`.
5. Integration: fake backend raising quota-exhausted on call 1 → stage result from backup; call 2 skips the down primary; cross-backend chain constructs the backup's backend; FailoverRecord logged + persisted on the run row; exhausted chain raises.
6. Real run via the `run-research-query` skill with a chain whose primary is forced unavailable (e.g. a bogus primary model id that `404`s) → run completes on the backup and the stored run shows the failover.

## Out of scope (deferred fast-follows)

- **Preflight availability pings** (decision 2 — reactive covers the real failure mode).
- **Cost ceiling / failover budget abort** (decision 4 — log-only in v1; a `max_failovers` or backend allow-list is the named fast-follow if cost surprise bites).
- **Automatic chain derivation** (e.g. "after primary, try the same role across the other presets") — chains are authored explicitly in v1 for predictability.
- **Mid-run recovery** of a model marked down (re-probing a backend whose quota may have freed up) — sticky-for-the-run is intentional in v1.
- **Cross-run / persistent availability state** (a backend known-down yesterday) — tracker is per-run only.

## Open questions for the `*.feedback` round

- **Parallel fan-out + shared tracker:** the researcher subgraph runs researchers concurrently via `asyncio.gather`. Should the `AvailabilityTracker` be shared across those tasks (one researcher discovering Gemini is down spares its siblings the failed call) or per-task? Shared is more efficient but introduces concurrent mutation of the down-set — needs a clear, race-safe contract.
- **Two retry layers:** `_run_with_retry` (transient, same-model) now sits *inside* the failover loop. Confirm the combined worst-case attempt count is bounded and sane (transient attempts × chain length) and doesn't multiply surprisingly.
- **`429` ground truth:** validate the transient-vs-hard `429` split against the *actual* error strings each CLI/SDK emits for throttle vs. quota-exhausted (Gemini CLI, Codex CLI, Claude Agent SDK) — the classifier is only as good as those matchers.
- **Chain authoring ergonomics:** is `str | list[str]` on the role the right surface, or should backups be a separate `"fallbacks"` map to keep the primary list visually clean? (Design proposes the inline list; revisit if reviewers find it muddies the preset.)
