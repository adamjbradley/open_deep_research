"""Render a MatrixResult to a stdout table, a markdown report, and a JSON blob.

Pure functions over the runner's dataclasses -- no I/O, no model calls -- so they're
trivially unit-testable. The markdown report ends with the actionable bits: best-fit model
per stage and a synthesised candidate NVIDIA routing preset.
"""
from __future__ import annotations

import json

from .runner import CellResult, MatrixResult, best_fit
from .stages import CONTRACT_PROSE, STAGES_BY_NAME

VALIDITY_THRESHOLD = 0.8
JUDGE_THRESHOLD = 7.0

# Graph role -> the probe stage that exercises it (for the candidate preset).
ROLE_STAGES = {
    "supervisor": "supervisor",
    "researcher": "researcher",
    "summarization": "summarize",
    "compression": "compress",
    "final_report": "final_report",
}


def _short(model: str) -> str:
    """A compact column label: drop the 'nvidia:' prefix and vendor namespace."""
    m = model.split(":", 1)[1] if ":" in model else model
    return m.split("/", 1)[1] if "/" in m else m


def _contract(stage: str) -> str:
    probe = STAGES_BY_NAME.get(stage)
    return probe.contract if probe else "structured"


def _headline(cell: CellResult) -> str:
    """The one-glance number for a cell: judge score for prose, else validity rate."""
    if cell.skipped_reason:
        return "skip"
    if cell.inconclusive:
        return "thr"  # all reps were throttle/timeout infra errors -- no capability signal
    if cell.contract == CONTRACT_PROSE:
        return f"{cell.judge_mean:.1f}" if cell.judge_mean is not None else "n/a"
    v = cell.validity_rate
    return f"{v:.2f}" if v is not None else "n/a"


def render_matrix(matrix: MatrixResult) -> str:
    """A fixed-width stage x model table (stages as rows). Cells = headline + p50 latency."""
    cols = [_short(m) for m in matrix.models]
    width = max([len("stage \\ model")] + [len(s) for s in matrix.stages]) + 2
    cwidth = max(11, max((len(c) for c in cols), default=11) + 2)

    def row(label: str, vals: list[str]) -> str:
        return label.ljust(width) + "".join(v.ljust(cwidth) for v in vals)

    lines = [row("stage \\ model", cols), row("-" * (width - 1), ["-" * (cwidth - 1)] * len(cols))]
    for stage in matrix.stages:
        vals = []
        for model in matrix.models:
            cell = matrix.cell(model, stage)
            head = _headline(cell)
            lat = cell.latency_p50
            vals.append(f"{head}@{lat:.1f}s" if lat is not None and head not in ("skip", "n/a") else head)
        lines.append(row(stage, vals))
    legend = ("\nlegend: capability stages = validity_rate (0-1); prose stages = judge score "
              "(0-10); '@Ns' = p50 latency; 'thr' = throttled/timeout (all reps infra-errored, "
              "no capability signal); 'skip' = backend marked down; 'n/a' = no data")
    return "\n".join(lines) + "\n" + legend


def _passes(matrix: MatrixResult, stage: str, model: str) -> bool:
    cell = matrix.cell(model, stage)
    if cell.contract == CONTRACT_PROSE:
        return cell.judge_mean is not None and cell.judge_mean >= JUDGE_THRESHOLD
    return cell.validity_rate is not None and cell.validity_rate >= VALIDITY_THRESHOLD


def best_fit_table(matrix: MatrixResult) -> list[dict]:
    """Per stage: the winning model, its score, and whether it clears the threshold."""
    out = []
    for stage in matrix.stages:
        contract = _contract(stage)
        winner = best_fit(matrix, stage, contract)
        if winner is None:
            out.append({"stage": stage, "contract": contract, "model": None,
                        "score": None, "clears_threshold": False})
            continue
        model, score = winner
        out.append({"stage": stage, "contract": contract, "model": model, "score": score,
                    "clears_threshold": _passes(matrix, stage, model)})
    return out


def candidate_preset(matrix: MatrixResult) -> dict:
    """A best-effort NVIDIA-only routing preset from the winners, with backup notes."""
    roles = {}
    notes = []
    for role, stage in ROLE_STAGES.items():
        if stage not in matrix.stages:
            continue
        winner = best_fit(matrix, stage, _contract(stage))
        if winner and _passes(matrix, stage, winner[0]):
            roles[role] = [winner[0], "claude-opus-4-8"]  # cross-backend backup retained
        else:
            best = f"{winner[0]} ({winner[1]})" if winner else "none"
            roles[role] = ["claude-opus-4-8"]
            notes.append(f"{role}: no NVIDIA model cleared threshold (best: {best}); kept Claude.")
    return {"roles": roles, "notes": notes}


def render_markdown(matrix: MatrixResult) -> str:
    bf = best_fit_table(matrix)
    lines = [
        "# NVIDIA per-stage role-fit benchmark",
        "",
        f"- models: {', '.join(matrix.models)}",
        f"- stages: {', '.join(matrix.stages)}",
        f"- reps per cell: {matrix.reps}",
        f"- thresholds: validity ≥ {VALIDITY_THRESHOLD}, judge ≥ {JUDGE_THRESHOLD}",
        "",
        "## Matrix",
        "",
        "```",
        render_matrix(matrix),
        "```",
        "",
        "## Per-cell detail",
        "",
        "| stage | model | contract | validity | judge | p50 | p95 | errors | skipped |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for stage in matrix.stages:
        for model in matrix.models:
            c = matrix.cell(model, stage)
            errs = ", ".join(f"{k}:{v}" for k, v in c.error_counts.items()) or "-"
            lines.append(
                f"| {stage} | {_short(model)} | {c.contract} "
                f"| {'-' if c.validity_rate is None else f'{c.validity_rate:.2f}'} "
                f"| {'-' if c.judge_mean is None else f'{c.judge_mean:.1f}'} "
                f"| {'-' if c.latency_p50 is None else f'{c.latency_p50:.1f}s'} "
                f"| {'-' if c.latency_p95 is None else f'{c.latency_p95:.1f}s'} "
                f"| {errs} | {c.skipped_reason or '-'} |")

    lines += ["", "## Best fit per stage", "",
              "| stage | contract | best model | score | clears threshold |",
              "|---|---|---|---|---|"]
    for row in bf:
        lines.append(
            f"| {row['stage']} | {row['contract']} | {_short(row['model']) if row['model'] else '—'} "
            f"| {'—' if row['score'] is None else row['score']} "
            f"| {'✅' if row['clears_threshold'] else '❌'} |")

    preset = candidate_preset(matrix)
    lines += ["", "## Candidate NVIDIA routing preset", "",
              "Winners that clear the threshold, each with a Claude cross-backend backup. "
              "Roles where no NVIDIA model qualified keep Claude.", "",
              "```json", json.dumps(preset["roles"], indent=2), "```"]
    if preset["notes"]:
        lines += ["", "**Notes:**", *[f"- {n}" for n in preset["notes"]]]
    return "\n".join(lines) + "\n"


def to_json(matrix: MatrixResult) -> dict:
    """Full serialisable record: every rep + aggregates, plus the derived recommendations."""
    cells = []
    for stage in matrix.stages:
        for model in matrix.models:
            c = matrix.cell(model, stage)
            cells.append({
                "model": model, "stage": stage, "contract": c.contract,
                "validity_rate": c.validity_rate, "judge_mean": c.judge_mean,
                "latency_p50": c.latency_p50, "latency_p95": c.latency_p95,
                "n": c.n, "n_ok": c.n_ok, "error_counts": c.error_counts,
                "skipped_reason": c.skipped_reason,
                "reps": [
                    {"ok": r.ok, "latency_s": r.latency_s, "error_class": r.error_class,
                     "error": r.error, "judge": r.judge}
                    for r in c.reps
                ],
            })
    return {
        "models": matrix.models, "stages": matrix.stages, "reps": matrix.reps,
        "thresholds": {"validity": VALIDITY_THRESHOLD, "judge": JUDGE_THRESHOLD},
        "cells": cells,
        "best_fit": best_fit_table(matrix),
        "candidate_preset": candidate_preset(matrix),
    }
