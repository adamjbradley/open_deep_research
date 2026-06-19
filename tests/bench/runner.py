"""Run the model x stage x reps matrix and aggregate per-cell metrics.

Pure orchestration + aggregation: the model builder, the judge, and the clock are all
injected, so this whole module is unit-testable offline with a fake model (no live calls).
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from open_deep_research.claude_agent_chat import build_chat_model
from open_deep_research.failover import classify_error

from .judge import judge_prose
from .stages import CONTRACT_PROSE, StageProbe, content_of

# Injected seams (defaults are the real ones; tests override).
BuildModel = Callable[..., Any]
Judge = Callable[..., Awaitable[Optional[dict]]]
Clock = Callable[[], float]


@dataclass
class RepResult:
    """One attempt at one (model, stage) cell."""

    ok: bool
    latency_s: float
    error_class: Optional[str] = None  # backend_fatal | model_fatal | transient
    error: Optional[str] = None
    judge: Optional[dict] = None


def _percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    # Nearest-rank, clamped -- fine for the small rep counts a benchmark uses.
    k = max(0, min(len(ordered) - 1, round((pct / 100.0) * (len(ordered) - 1))))
    return round(ordered[k], 3)


@dataclass
class CellResult:
    """All reps for one (model, stage) cell, plus aggregate metrics."""

    model: str
    stage: str
    contract: str
    reps: list[RepResult] = field(default_factory=list)
    skipped_reason: Optional[str] = None
    backend_fatal: bool = False

    @property
    def n(self) -> int:
        return len(self.reps)

    @property
    def n_ok(self) -> int:
        return sum(1 for r in self.reps if r.ok)

    @property
    def n_counted(self) -> int:
        """Reps that are a real capability data point: returned a response, OR were rejected
        by the model itself (model_fatal = wrong/removed id). Transient infra errors
        (throttle/timeout) and backend-fatal aborts are NOT capability signals -- excluded."""
        return sum(1 for r in self.reps if r.error_class in (None, "model_fatal"))

    @property
    def inconclusive(self) -> bool:
        """The cell ran but every rep was an infra error (throttle/timeout) -- no capability
        signal. Distinct from a genuine 0.0 (the model returned, but always wrong)."""
        return bool(self.reps) and not self.skipped_reason and self.n_counted == 0

    @property
    def validity_rate(self) -> Optional[float]:
        """Fraction of capability-relevant reps that satisfied the contract.

        None when the cell was skipped or inconclusive (all reps were throttle/timeout
        infra errors) -- so rate-limiting never masquerades as a 0.0 capability score.
        """
        if self.skipped_reason or self.n_counted == 0:
            return None
        return round(self.n_ok / self.n_counted, 3)

    @property
    def latency_p50(self) -> Optional[float]:
        return _percentile([r.latency_s for r in self.reps], 50)

    @property
    def latency_p95(self) -> Optional[float]:
        return _percentile([r.latency_s for r in self.reps], 95)

    @property
    def error_counts(self) -> dict[str, int]:
        return dict(Counter(r.error_class for r in self.reps if r.error_class))

    @property
    def judge_mean(self) -> Optional[float]:
        scores = [r.judge["score"] for r in self.reps if r.judge and "score" in r.judge]
        return round(sum(scores) / len(scores), 2) if scores else None


@dataclass
class MatrixResult:
    """The whole run: every cell keyed by (model, stage), plus run metadata."""

    models: list[str]
    stages: list[str]
    reps: int
    cells: dict[tuple[str, str], CellResult]

    def cell(self, model: str, stage: str) -> CellResult:
        return self.cells[(model, stage)]


def matrix_from_json(data: dict) -> MatrixResult:
    """Rebuild a MatrixResult from a ``report.to_json`` blob's raw per-rep records.

    Lets a finished run be re-aggregated with the current metric logic (e.g. to re-derive
    honest validity after fixing how throttle errors are counted) without re-running it.
    """
    cells: dict[tuple[str, str], CellResult] = {}
    for c in data.get("cells", []):
        reps = [RepResult(ok=r["ok"], latency_s=r["latency_s"],
                          error_class=r.get("error_class"), error=r.get("error"),
                          judge=r.get("judge")) for r in c.get("reps", [])]
        cells[(c["model"], c["stage"])] = CellResult(
            model=c["model"], stage=c["stage"], contract=c["contract"], reps=reps,
            skipped_reason=c.get("skipped_reason"))
    return MatrixResult(models=data["models"], stages=data["stages"],
                        reps=data.get("reps", 0), cells=cells)


async def run_cell(
    model_string: str,
    probe: StageProbe,
    reps: int,
    *,
    build_model: BuildModel = build_chat_model,
    judge: Optional[Judge] = judge_prose,
    judge_build_model: Optional[BuildModel] = None,
    clock: Clock = time.perf_counter,
) -> CellResult:
    """Run ``reps`` attempts of one stage against one model and aggregate.

    A ``backend_fatal`` error (auth/quota for the whole backend) stops this cell early and
    sets ``backend_fatal`` so the caller can skip the model's remaining stages.
    """
    cell = CellResult(model=model_string, stage=probe.name, contract=probe.contract)
    runnable = probe.apply(build_model(model_string, max_tokens=probe.max_tokens))

    for _ in range(reps):
        t0 = clock()
        try:
            resp = await runnable.ainvoke(probe.messages())
            latency = clock() - t0
            ok = bool(probe.is_valid(resp))
            judged = None
            if ok and probe.contract == CONTRACT_PROSE and judge is not None:
                judged = await judge(
                    probe.name, probe.judge_context(), content_of(resp),
                    build_model=judge_build_model or build_model)
            cell.reps.append(RepResult(ok=ok, latency_s=round(latency, 3), judge=judged))
        except Exception as exc:  # noqa: BLE001 - benchmarking records failures, never raises
            latency = clock() - t0
            kind = classify_error(exc)
            cell.reps.append(RepResult(
                ok=False, latency_s=round(latency, 3), error_class=kind, error=str(exc)[:200]))
            if kind == "backend_fatal":
                cell.backend_fatal = True
                break
    return cell


async def run_matrix(
    models: list[str],
    probes: list[StageProbe],
    reps: int,
    *,
    build_model: BuildModel = build_chat_model,
    judge: Optional[Judge] = judge_prose,
    judge_build_model: Optional[BuildModel] = None,
    clock: Clock = time.perf_counter,
    on_cell: Optional[Callable[[CellResult], None]] = None,
) -> MatrixResult:
    """Run every model x stage cell. Once a model hits a backend-fatal error, its remaining
    stages are skipped (marked, not silently dropped) so the run doesn't burn budget on a
    backend that's already down."""
    cells: dict[tuple[str, str], CellResult] = {}
    dead: dict[str, str] = {}  # model -> reason it was marked down

    for model in models:
        for probe in probes:
            if model in dead:
                cell = CellResult(model=model, stage=probe.name, contract=probe.contract,
                                  skipped_reason=dead[model])
            else:
                cell = await run_cell(
                    model, probe, reps, build_model=build_model, judge=judge,
                    judge_build_model=judge_build_model, clock=clock)
                if cell.backend_fatal:
                    dead[model] = f"backend marked down at stage '{probe.name}'"
            cells[(model, probe.name)] = cell
            if on_cell is not None:
                on_cell(cell)

    return MatrixResult(models=list(models), stages=[p.name for p in probes],
                        reps=reps, cells=cells)


# -- recommendation logic -----------------------------------------------------
def best_fit(matrix: MatrixResult, stage: str, contract: str) -> Optional[tuple[str, float]]:
    """The best model for a stage and its headline score.

    Capability stages rank by (validity_rate desc, latency asc); prose ranks by judge score.
    Returns (model, score) or None if no cell produced a usable number.
    """
    ranked: list[tuple[float, float, str]] = []
    for model in matrix.models:
        c = matrix.cell(model, stage)
        if contract == CONTRACT_PROSE:
            primary = c.judge_mean
        else:
            primary = c.validity_rate
        if primary is None:
            continue
        # Lower latency breaks ties; missing latency sorts last.
        lat = c.latency_p50 if c.latency_p50 is not None else float("inf")
        ranked.append((primary, -lat, model))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    primary, _neg_lat, model = ranked[0]
    return model, primary
