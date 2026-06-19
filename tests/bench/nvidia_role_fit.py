# ruff: noqa: T201 - this is a CLI; stdout via print() is the intended interface.
"""CLI: benchmark which NVIDIA model best fits each deep-research graph LLM-call.

    uv run python -m tests.bench.nvidia_role_fit                 # full matrix, reps=5
    uv run python -m tests.bench.nvidia_role_fit --dry-run       # list cells, fire nothing
    uv run python -m tests.bench.nvidia_role_fit --models nvidia:z-ai/glm-5.1 --stages supervisor,extract_facts --reps 3

Live + paid + non-deterministic. Requires NVIDIA_API_KEY (see .env.example). Writes a
timestamped JSON + markdown report under tests/bench/results/ and prints the matrix.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .report import render_markdown, render_matrix, to_json
from .runner import run_matrix
from .stages import STAGES, STAGES_BY_NAME

DEFAULT_MODELS = [
    "nvidia:nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia:minimaxai/minimax-m3",
    "nvidia:minimaxai/minimax-m2.7",
    "nvidia:moonshotai/kimi-k2.6",
    "nvidia:z-ai/glm-5.1",
    "nvidia:deepseek-ai/deepseek-v4-pro",
    "nvidia:deepseek-ai/deepseek-v4-flash",
]
_RESULTS_DIR = Path(__file__).parent / "results"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NVIDIA per-stage role-fit benchmark")
    p.add_argument("--models", help="comma-separated model strings (default: the 7 NVIDIA ids)")
    p.add_argument("--stages", help=f"comma-separated stages (default: all; available: "
                                    f"{','.join(STAGES_BY_NAME)})")
    p.add_argument("--reps", type=int, default=5, help="reps per cell (default 5)")
    p.add_argument("--out", default=str(_RESULTS_DIR), help="output directory")
    p.add_argument("--dry-run", action="store_true", help="list the cells and exit; fire nothing")
    p.add_argument("--from-json", help="re-render a finished run's JSON with current metric "
                                       "logic (no live calls); prints matrix + writes a report")
    return p.parse_args(argv)


def _select_stages(spec: str | None):
    if not spec:
        return list(STAGES)
    chosen = []
    for name in [s.strip() for s in spec.split(",") if s.strip()]:
        if name not in STAGES_BY_NAME:
            raise SystemExit(f"unknown stage {name!r}; available: {', '.join(STAGES_BY_NAME)}")
        chosen.append(STAGES_BY_NAME[name])
    return chosen


async def _amain(args: argparse.Namespace) -> int:
    if args.from_json:
        from .runner import matrix_from_json
        data = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        matrix = matrix_from_json(data)
        print(render_matrix(matrix) + "\n")
        out = Path(args.from_json).with_suffix(".reagg.md")
        out.write_text(render_markdown(matrix), encoding="utf-8")
        print(f"re-rendered -> {out}")
        return 0

    models = ([m.strip() for m in args.models.split(",") if m.strip()]
              if args.models else list(DEFAULT_MODELS))
    probes = _select_stages(args.stages)

    if args.dry_run:
        print(f"DRY RUN: {len(models)} models x {len(probes)} stages x {args.reps} reps "
              f"= {len(models) * len(probes) * args.reps} live calls (+ prose judging)")
        for m in models:
            print(f"  {m}")
            for p in probes:
                print(f"      - {p.name} [{p.contract}]")
        return 0

    if not os.getenv("NVIDIA_API_KEY"):
        print("ERROR: NVIDIA_API_KEY is not set; required for the nvidia: backend. "
              "Put it in .env (see .env.example).", file=sys.stderr)
        return 2

    total = len(models) * len(probes)
    print(f"Running {total} cells ({len(models)} models x {len(probes)} stages), "
          f"{args.reps} reps each ...\n")

    done = {"n": 0}

    def progress(cell):
        done["n"] += 1
        head = (f"validity={cell.validity_rate}" if cell.contract != "prose"
                else f"judge={cell.judge_mean}")
        flag = f" SKIPPED ({cell.skipped_reason})" if cell.skipped_reason else ""
        print(f"  [{done['n']}/{total}] {cell.model} :: {cell.stage} -> {head} "
              f"p50={cell.latency_p50}{flag}")

    matrix = await run_matrix(models, probes, args.reps, on_cell=progress)

    print("\n" + render_matrix(matrix) + "\n")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"{stamp}_nvidia_fit.json"
    md_path = out_dir / f"{stamp}_nvidia_fit.md"
    json_path.write_text(json.dumps(to_json(matrix), indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(matrix), encoding="utf-8")
    print(f"wrote {json_path}\nwrote {md_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(_parse_args(argv if argv is not None else sys.argv[1:])))


if __name__ == "__main__":
    raise SystemExit(main())
