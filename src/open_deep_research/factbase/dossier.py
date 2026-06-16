"""Dossier CLI: read-only views over the fact base (show / compare)."""
from __future__ import annotations

import argparse
import asyncio

import aiosqlite

from . import metrics as _metrics, query as _query, render as _render
from .entities import CountryResolver
from open_deep_research.storage import get_db_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dossier", description="Inspect the living fact base.")
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", help="Show all facts for a country.")
    show.add_argument("country")
    show.add_argument("--format", choices=["text", "md", "csv"], default="text")
    show.add_argument("--raw", action="store_true", help="One row per source (no canonical grouping).")

    compare = sub.add_parser("compare", help="Compare a property across all instances.")
    compare.add_argument("property")
    compare.add_argument("--format", choices=["text", "md", "csv"], default="text")
    compare.add_argument("--raw", action="store_true", help="One row per source (no canonical grouping).")

    sub.add_parser("stats", help="Fact-base health metrics")

    return parser


async def run(argv, db_path=None) -> str:
    args = _parser().parse_args(argv)
    db_path = db_path or get_db_path(None)
    async with aiosqlite.connect(db_path) as conn:
        q = _query.FactQuery(conn)
        if args.command == "show":
            key = CountryResolver().resolve(args.country)
            if key is None:
                return f"Unknown country: {args.country!r} (could not resolve to a canonical key)."
            rows = await (q.show(key) if args.raw else q.show_grouped(key))
            return _render.render(rows, fmt=args.format)
        if args.command == "compare":
            rows = await (q.compare(args.property) if args.raw else q.compare_grouped(args.property))
            return _render.render(rows, fmt=args.format)
        if args.command == "stats":
            m = await _metrics.compute(conn)
            return "\n".join(f"{k}: {v}" for k, v in m.items())
    raise ValueError(f"unknown command: {args.command!r}")


def main():
    import sys
    print(asyncio.run(run(sys.argv[1:])))


if __name__ == "__main__":
    main()
