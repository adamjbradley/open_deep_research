# NVIDIA per-stage role-fit benchmark — design

**Date:** 2026-06-19
**Status:** Approved (brainstorming) → implementation

## Goal

A standalone, on-demand **live** benchmark that fires each deep-research graph LLM-call's
*actual contract* against each NVIDIA model, producing a **fitness matrix** and a
**recommended best-fit model per stage**. Answers: "where do these NVIDIA models shine (if
at all), and which is the best fit for each graph role?"

Not a CI test — it is slow, paid, and non-deterministic. Runs only when invoked.

## Decisions (from brainstorming)

1. **Per-stage capability matrix** — fire each stage's real prompt + schema/tools directly
   (not a full graph run), so failures are attributable and cheap.
2. **NVIDIA models only** — the 7 ids already wired behind the `nvidia:` prefix.
3. **Standalone script + saved report** — `uv run python -m tests.bench.nvidia_role_fit`;
   prints a matrix, writes timestamped JSON + markdown under `tests/bench/results/`.
4. **Claude LLM judge for prose** — fixed grounding/relevance/coherence rubric; the local
   Claude (subscription) is the judge only, not a compared model.

## Models (pool)

`nvidia/nemotron-3-ultra-550b-a55b`, `minimaxai/minimax-m3`, `minimaxai/minimax-m2.7`,
`moonshotai/kimi-k2.6`, `z-ai/glm-5.1`, `deepseek-ai/deepseek-v4-pro`,
`deepseek-ai/deepseek-v4-flash` — each addressed as `nvidia:<id>`.

## Stages (probes)

Each probe wraps a **real** graph seam: it imports the codebase's actual schema / tools /
prompt — never reimplements them. Inputs are small, fixed, checked-in fixtures so runs are
comparable across models and across days.

| Stage | Contract | Real seam |
|---|---|---|
| `supervisor` | tool-calling | `.bind_tools([ConductResearch, ResearchComplete, think_tool])` |
| `researcher` | tool-calling | `.bind_tools([<search tool>, think_tool])` |
| `research_brief` | structured | `.with_structured_output(ResearchQuestion)` |
| `assess_knowledge` | structured | `.with_structured_output(KnowledgeAssessment)` |
| `target_properties` | structured | `.with_structured_output(TargetProperties)` (real Profile) |
| `extract_facts` | structured-text | `build_extraction_prompt(prof,…)` → plain invoke → `parse_lean_facts` |
| `summarize` | prose | `summarize_webpage_prompt` |
| `compress` | prose | `compress_research_system_prompt` |
| `final_report` | prose | `final_report_generation_prompt` |

`extract_facts` is a "structured-text" contract: the graph invokes plain text and parses
leniently with `parse_lean_facts`, so validity = "≥1 valid LeanFact parsed".

## Metrics (per model × stage cell)

- `validity_rate` over N reps:
  - structured: `.with_structured_output` returns a schema-valid object (no exception).
  - tool: response has ≥1 `tool_call` whose `name` is one of the bound tools.
  - structured-text (`extract_facts`): `parse_lean_facts(content)` returns ≥1 record.
  - prose: non-empty content.
- `latency_p50` / `latency_p95` (seconds), per cell.
- `errors`: counts by class via the existing `failover.classify_error`
  (`backend_fatal` / `model_fatal` / `transient`).
- prose only: `judge_score` (mean 0–10) from the Claude judge (grounding/relevance/coherence).

## Architecture (modules under `tests/bench/`)

Each module has one job and a narrow interface:

- `stages.py` — `StageProbe` dataclass + a `STAGES` registry. A probe exposes:
  `name`, `contract` (`"tool" | "structured" | "structured_text" | "prose"`),
  `build(model_string, max_tokens) -> Runnable`, `messages() -> list`,
  `is_valid(response) -> bool`. Pulls real schemas/tools/prompts from the codebase.
- `judge.py` — `judge_prose(stage, prompt, output) -> JudgeResult` using Claude
  (`build_chat_model("claude:haiku")` or configured) with a fixed rubric + structured output.
- `runner.py` — `run_cell(model, probe, reps) -> CellResult` and
  `run_matrix(models, probes, reps) -> MatrixResult`. Bounded concurrency; each rep wrapped;
  a `backend_fatal` error short-circuits that model's remaining cells (logged). Pure metric
  aggregation lives here and is unit-testable with a fake model.
- `report.py` — `render_matrix(MatrixResult) -> str` (stdout table),
  `render_markdown(MatrixResult) -> str` (full report + best-fit per stage + a synthesized
  candidate NVIDIA preset), `to_json(MatrixResult) -> dict`. Pure functions.
- `nvidia_role_fit.py` — `__main__` CLI: `--models --stages --reps (default 5) --out
  --dry-run`. Orchestrates, prints, writes `results/<ts>_nvidia_fit.{json,md}`.
- `results/` — output dir, git-ignored (keep `.gitkeep`).

## Recommendation logic

Per stage: rank models by `(validity_rate desc, latency_p50 asc)` for capability stages;
by `judge_score desc` for prose. Emit best-fit per stage and assemble a candidate NVIDIA
routing preset, flagging any stage where no model clears a threshold (default validity
≥ 0.8 / judge ≥ 7.0) → note "keep Claude/Gemini backup".

## Cost / safety controls

- Requires `NVIDIA_API_KEY` (clear error if absent).
- `--dry-run` lists every cell that would run and fires nothing.
- `--models / --stages / --reps` narrow a run; modest `max_tokens` per probe.
- Skipped/failed cells are always shown in the report — never silently dropped.
- Never imported by CI; lives under `tests/bench/`, not collected as a normal test
  (filename not `test_*`).

## Harness self-test (offline, free)

`tests/test_bench_harness.py` — mocked/fake model (no API):
- `runner.run_matrix` aggregates validity/latency/errors correctly (incl. a
  `backend_fatal` short-circuit and a model that throws).
- `report.render_matrix` / `render_markdown` produce a stable table + a best-fit pick.
- each `StageProbe.is_valid` accepts a good response and rejects a bad one.

This keeps the harness logic honest without paying for live calls.

## Out of scope

Full graph end-to-end runs; non-NVIDIA models; CI gating; per-rep caching; cost-in-dollars
estimation (we report latency + token caps, not billing).
