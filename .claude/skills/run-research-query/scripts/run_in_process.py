"""Run a prompt through the deep_researcher graph in-process (Path B).

Loads the on-disk graph and invokes it directly -- no langgraph server, no auth,
no host networking. Bills the Claude subscription (clears ANTHROPIC_API_KEY so the
Agent SDK doesn't silently route to paid API billing). Prints the report plus
notes/raw_notes counts, so it also works as a fan-out smoke test.

Usage:
    uv run python run_in_process.py "What do you know about the Eiffel Tower"
    uv run python run_in_process.py "..." --kb-off --iterations 1
    uv run python run_in_process.py "..." --db /path/to/research_results.db --full

Options:
    --kb-off        Disable the knowledge base so the run always does fresh research
                    (skips the cache short-circuit). Also implies no clarification.
    --iterations N  max_researcher_iterations (default 2). Use 2 or more: the
                    supervisor's premature-completion guard spends the first turn on a
                    corrective nudge, so --iterations 1 leaves no turn to actually
                    research (the report then comes from the final-report model alone).
    --db PATH       SQLite file to use. Default: an isolated temp DB (won't touch the
                    project's research_results.db). Pass the project DB to accumulate.
    --full          Use the graph's default limits (deeper, slower) instead of the
                    reduced defaults this script applies for quick runs.
    --no-persist    Don't write to any SQLite DB.
"""
import argparse
import asyncio
import os
import sys
import tempfile
import uuid

# Subscription mode: an API key would re-route the Agent SDK to paid API billing.
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ.setdefault("CLAUDE_USE_SUBSCRIPTION", "true")

from langchain_core.messages import HumanMessage  # noqa: E402
from open_deep_research.deep_researcher import deep_researcher  # noqa: E402


async def main(args: argparse.Namespace) -> None:
    configurable = {"thread_id": str(uuid.uuid4())}
    if not args.full:
        configurable.update(
            max_concurrent_research_units=2,
            max_researcher_iterations=args.iterations,
            max_react_tool_calls=4,
        )
    if args.kb_off:
        configurable.update(use_knowledge_base=False, allow_clarification=False)
    if args.no_persist:
        configurable["persist_results"] = False
    configurable["database_path"] = args.db or os.path.join(
        tempfile.gettempdir(), "odr_in_process.db"
    )

    print(f"Prompt: {args.prompt}\nRunning (in-process)...\n", flush=True)
    # Scale the super-step budget to the iteration cap so a long run doesn't crash with
    # GraphRecursionError (LangGraph's default is 25; the supervisor loop alone can exceed it).
    from open_deep_research.deep_researcher import recommended_recursion_limit
    recursion_limit = recommended_recursion_limit(
        configurable.get("max_researcher_iterations", 6),
        configurable.get("max_concurrent_research_units", 1),
    )
    result = await deep_researcher.ainvoke(
        {"messages": [HumanMessage(content=args.prompt)]},
        config={"configurable": configurable, "recursion_limit": recursion_limit},
    )

    print("=" * 60)
    print(
        f"subject: {result.get('subject')!r} | "
        f"cached: {result.get('answered_from_cache')} | "
        f"report_id: {result.get('report_id')}"
    )
    print(f"raw_notes: {len(result.get('raw_notes', []))} | notes: {len(result.get('notes', []))}")
    print("=" * 60)
    report = result.get("final_report") or ""
    if report:
        print(report)
    else:
        msgs = result.get("messages") or []
        print(getattr(msgs[-1], "content", "(no report)") if msgs else "(no report)")


def parse_args(argv: list) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run a prompt through deep_researcher in-process.")
    p.add_argument("prompt", nargs="+", help="The research question.")
    p.add_argument("--kb-off", action="store_true", help="Disable the knowledge base (fresh research).")
    p.add_argument("--iterations", type=int, default=2, help="max_researcher_iterations (default 2; use >=2, see module docstring).")
    p.add_argument("--db", help="SQLite DB path (default: isolated temp DB).")
    p.add_argument("--full", action="store_true", help="Use the graph's default (deeper) limits.")
    p.add_argument("--no-persist", action="store_true", help="Do not write to any SQLite DB.")
    ns = p.parse_args(argv)
    ns.prompt = " ".join(ns.prompt).strip()
    return ns


if __name__ == "__main__":
    asyncio.run(main(parse_args(sys.argv[1:])))
