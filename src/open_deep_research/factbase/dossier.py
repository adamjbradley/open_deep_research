"""Dossier CLI: read-only views over the fact base (show / compare)."""
from __future__ import annotations

import argparse
import asyncio

import aiosqlite

from . import metrics as _metrics, query as _query, render as _render
from .entities import CountryResolver
from open_deep_research.storage import get_db_path


def validate_profiles(extra_paths=None) -> tuple[str, bool]:
    """Validate every shipped profile/registry YAML (plus any extra_paths). Returns (report, ok).

    A registry file is any whose top-level YAML is a dict with a 'sources' key; everything
    else is treated as a profile.
    """
    import yaml
    from importlib.resources import files as _files
    from .profile_schema import profile_from_dict
    from .registry_schema import registry_from_dict

    paths = []
    pkg = _files("open_deep_research.factbase.profiles")
    for entry in pkg.iterdir():
        if entry.name.endswith(".yaml") and not entry.name.endswith(".draft.yaml"):
            paths.append(entry)
    paths.extend(extra_paths or [])

    lines, ok = [], True
    for path in paths:
        name = getattr(path, "name", str(path))
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "sources" in data:
                registry_from_dict(data)
            else:
                profile_from_dict(data)
            lines.append(f"OK    {name}")
        except Exception as e:  # noqa: BLE001 - report-and-continue is the point
            ok = False
            lines.append(f"FAIL  {name}: {e}")
    return "\n".join(lines), ok


def _scaffold_model_call():
    """Return an async model_call(prompt) -> ScaffoldProposal using the configured model.

    Overridable in tests so the CLI needs no LLM/network.
    """
    from langchain_core.messages import HumanMessage
    from open_deep_research.deep_researcher import configurable_model
    from .scaffold import ScaffoldProposal

    async def call(prompt: str):
        model = configurable_model.with_structured_output(ScaffoldProposal)
        return await model.ainvoke([HumanMessage(content=prompt)])
    return call


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
    sub.add_parser("validate", help="Validate all profile/registry YAML files.")

    rec = sub.add_parser("recompute", help="Recompute canonical fact values; --check reports drift only.")
    rec.add_argument("--profile", default="country_digital_identity",
                     help="Profile name (YAML stem) to check/recompute against.")
    rec.add_argument("--check", action="store_true",
                     help="Report whether the profile changed since the last stamped run (no writes).")
    rec.add_argument("--rebuild", action="store_true",
                     help="Structural rebuild: re-derive tuple_key, conflicts, promotion (after identity/enum/threshold edits).")
    rec.add_argument("--rename", action="append", default=[], metavar="OLD=NEW",
                     help="Rename a property during rebuild (repeatable).")
    rec.add_argument("--on-removed", choices=["retain", "soft_delete"], default="retain",
                     help="Policy for facts whose property was removed from the profile.")

    sc = sub.add_parser("scaffold", help="Generate a usable profile for a domain (+ an annotated comparison draft).")
    sc.add_argument("entity_type")
    sc.add_argument("description")
    sc.add_argument("--out", help="Usable profile path (default factbase/profiles/<slug>.yaml).")
    sc.add_argument("--seed", action="append", default=[], metavar="URL",
                    help="Seed source URL(s) to ground the schema in real vocabulary (fetched as data; repeatable).")

    mx = sub.add_parser("matrix", help="Cross-country matrix: rows=instances, cols=profile properties.")
    mx.add_argument("--profile", default="country_digital_identity",
                    help="Profile whose properties form the matrix columns.")
    mx.add_argument("--format", choices=["text", "md", "csv"], default="text")

    bt = sub.add_parser("batch", help="Run a profile across many countries (resumable).")
    bt.add_argument("--profile", required=True)
    bt.add_argument("--countries", help="Explicit 'A,B,C', @file, or a group name (e.g. G20).")
    bt.add_argument("--scout", help="Discover the country list from this query instead.")
    bt.add_argument("--concurrency", type=int, default=3)
    bt.add_argument("--format", choices=["text", "md", "csv"], default="text")
    bt.add_argument("--no-registry-autoprovision", action="store_true",
                    help="Accepted; registry auto-provision is not performed in CLI mode "
                         "(it runs via the batch API). Reserved for parity.")
    bt.add_argument("--dry-run", action="store_true",
                    help="Resolve the list (+report unresolved) without running research.")

    return parser


async def run(argv, db_path=None) -> str:
    args = _parser().parse_args(argv)
    if args.command == "validate":
        report, ok = validate_profiles()
        return report if ok else report + "\nINVALID"
    db_path = db_path or get_db_path(None)
    if args.command == "recompute":
        from open_deep_research import storage as _storage
        from open_deep_research.factbase import (
            drift as _drift, migrations as _mig, profile as _profile,
            recompute as _recompute, schema as _schema,
        )
        prof = _profile.load(args.profile)
        cur_hash = getattr(prof, "profile_hash", None)
        if getattr(args, "rebuild", False):
            from open_deep_research.factbase import rebuild as _rebuild, registry as _registry
            reg = _registry.SourceRegistry.load(args.registry if hasattr(args, "registry") else "di_source_registry")
            rename = dict(pair.split("=", 1) for pair in args.rename) if args.rename else {}
            async with aiosqlite.connect(db_path) as conn:
                await _storage._ensure_schema(conn)
                await _mig.apply(conn, _schema.STEPS)
                stats = await _rebuild.rebuild_structural(
                    conn, prof, reg, rename=rename, on_removed=args.on_removed)
            return ("rebuild complete for " + args.profile + ": "
                    + ", ".join(f"{k}={v}" for k, v in stats.items()))
        if args.check:
            d = await _drift.check_drift(db_path, args.profile, cur_hash)
            if d["drifted"]:
                return (f"DRIFT  {args.profile}: changed since last run "
                        f"({(d['last_run_hash'] or '')[:8]} -> {(cur_hash or '')[:8]}). "
                        f"Run `dossier recompute --profile {args.profile}` to refresh canonical values.")
            return f"OK     {args.profile}: no drift (hash {(cur_hash or 'none')[:8]})."
        async with aiosqlite.connect(db_path) as conn:
            await _storage._ensure_schema(conn)
            await _mig.apply(conn, _schema.STEPS)
            n = await _recompute.backfill_canonical_values(conn, prof, force=True)
        return f"recomputed canonical values for {n} fact row(s) under {args.profile}."
    if args.command == "scaffold":
        import os
        from open_deep_research.storage import slugify
        from .scaffold import (
            existing_property_names_for, induce, render_draft_yaml, render_profile_yaml)
        from . import fetch as _fetch
        sources = []
        for url in (getattr(args, "seed", None) or []):
            txt = await _fetch.fetch_text(url)  # SSRF-safe; returns None on any failure
            if txt:
                sources.append(txt)
        existing = existing_property_names_for(args.entity_type)  # reuse the entity type; only add new props
        proposal = await induce(args.entity_type, args.description, sources, existing, _scaffold_model_call())
        out_yaml = args.out or os.path.join(
            os.path.dirname(__file__), "profiles", f"{slugify(args.description)}.yaml")
        out_draft = (out_yaml[:-5] + ".draft.yaml") if out_yaml.endswith(".yaml") else out_yaml + ".draft.yaml"
        replaced = os.path.exists(out_yaml)
        with open(out_yaml, "w", encoding="utf-8") as fh:
            fh.write(render_profile_yaml(proposal))
        with open(out_draft, "w", encoding="utf-8") as fh:
            fh.write(render_draft_yaml(proposal))
        note = " (replaced existing)" if replaced else ""
        return (f"Wrote usable profile {out_yaml}{note} -- live now -- and annotated comparison copy "
                f"{out_draft} (not loaded). Diff them to review what the generator decided.")
    if args.command == "matrix":
        from open_deep_research import storage as _storage
        from open_deep_research.factbase import migrations as _mig, schema as _schema
        from .matrix import render_matrix
        from .profile import load as _load_profile
        prof = _load_profile(args.profile)
        property_names = [pd.name for pd in prof.properties]
        resolver = CountryResolver()
        async with aiosqlite.connect(db_path) as conn:
            await _storage._ensure_schema(conn)
            await _mig.apply(conn, _schema.STEPS)
            q = _query.FactQuery(conn)
            rows = []
            for name in property_names:
                rows.extend(await q.compare_grouped(name))
        if not rows:
            return f"No facts found for profile '{args.profile}' in the database."
        return render_matrix(rows, property_names, resolver.instance_name, fmt=args.format)
    if args.command == "batch":
        from open_deep_research import storage as _storage
        from open_deep_research.factbase import migrations as _mig, schema as _schema
        from .country_list import resolve_country_list
        from .profile import load as _load_profile
        prof = _load_profile(args.profile)
        if args.scout:
            return ("scout discovery runs only via the batch API (needs a model call); "
                    "pass --countries for the CLI, or call BatchRunner with a scout_call.")
        names = resolve_country_list(args.countries) if args.countries else []
        if not names:
            return "error: batch needs a country list — pass --countries 'A,B,C'|@file|<group> (or --scout)."
        resolver = CountryResolver()
        if args.dry_run:
            lines = []
            for n in names:
                k = resolver.resolve(n)
                lines.append(f"  {n} -> {k}" if k else f"  {n} -> UNRESOLVED")
            return f"dry-run: {len(names)} countries for {args.profile}\n" + "\n".join(lines)
        from .batch import BatchRunner, default_run_one
        from .matrix import render_matrix
        runner = BatchRunner(profile_name=args.profile, db_path=db_path,
                             concurrency=args.concurrency, run_one=default_run_one,
                             profile_hash=getattr(prof, "profile_hash", ""),
                             list_spec=args.countries or "")
        res = await runner.run(names)
        property_names = [pd.name for pd in prof.properties]
        async with aiosqlite.connect(db_path) as conn:
            await _storage._ensure_schema(conn)
            await _mig.apply(conn, _schema.STEPS)
            q = _query.FactQuery(conn)
            rows = []
            for nm in property_names:
                rows.extend(await q.compare_grouped(nm))
        matrix = render_matrix(rows, property_names, resolver.instance_name, fmt=args.format)
        summary = ", ".join(f"{k}={v}" for k, v in sorted(res["summary"].items()))
        unresolved = (" | unresolved: " + ", ".join(res["unresolved"])) if res["unresolved"] else ""
        return f"batch {res['batch_id']}: {summary}{unresolved}\n\n{matrix}"
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
    out = asyncio.run(run(sys.argv[1:]))
    print(out)
    if out.endswith("INVALID"):
        sys.exit(1)


if __name__ == "__main__":
    main()
