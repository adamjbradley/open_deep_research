"""Expand a CLI country-list spec into a list of country names.

Three input shapes (scout discovery lives in Plan 7b, where the model call is):
  - "@/path/to/file"   one country name per line
  - a known group name ("G20", "EU", "West Africa") -> its members
  - a comma-separated list of names ("A, B, C")
A single bare token that is not a known group is treated as one explicit name.

TRUST BOUNDARY: ``spec`` (including the ``@file`` form) must come ONLY from a
CLI-authenticated operator argument, never from LLM-generated or remote input. The
``@file`` branch opens the given path verbatim by design (a standard CLI affordance,
like ``curl @file``); the operator already has shell-level read access, so this grants
no escalation. Do NOT route scout/LLM output or untrusted request data through here —
the scout strategy (Plan 7b) returns names directly and must not reach this open().
"""
from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def _load_groups() -> dict[str, list[str]]:
    import yaml
    from importlib.resources import files

    text = files("open_deep_research.factbase.data").joinpath("groups.yaml").read_text(
        encoding="utf-8")
    return yaml.safe_load(text) or {}


def resolve_country_list(spec: str) -> list[str]:
    """Expand a country-list ``spec`` into country names.

    ``spec`` is one of: ``@/path/to/file`` (one name per line), a named group key
    (``G20``/``EU``/``West Africa``), or a comma-separated name list. Raises
    ``ValueError`` if ``spec`` is blank or an ``@file`` yields no names.
    """
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("empty country-list spec")
    if spec.startswith("@"):
        # Operator-supplied path (see module TRUST BOUNDARY note): opened verbatim by design.
        with open(spec[1:], encoding="utf-8") as fh:
            names = [ln.strip() for ln in fh]
        out = [n for n in names if n]
        if not out:
            raise ValueError(f"no country names in file {spec[1:]}")
        return out
    # A comma means an explicit list; this relies on group names never containing a
    # comma (true for groups.yaml today — keep it that way when adding groups).
    if "," not in spec:
        groups = _load_groups()
        if spec in groups:
            return list(groups[spec])
        return [spec]  # a single explicit name
    return [part.strip() for part in spec.split(",") if part.strip()]


async def resolve_country_list_async(*, spec, scout_query, scout_call) -> list[str]:
    """Async country-list resolution with optional LLM scout discovery.

    If ``scout_query`` is given, discover names via ``scout_call(query) -> list[str]``;
    otherwise delegate to the synchronous :func:`resolve_country_list` on ``spec``.

    The scout path is the one place LLM output enters list resolution; it returns NAMES
    only (each later resolved to an instance_key by the caller) and never touches the
    ``@file`` open() path (see the module TRUST BOUNDARY note).
    """
    if scout_query:
        if scout_call is None:
            raise ValueError("scout_query given but no scout_call provided")
        names = await scout_call(scout_query)
        return [n.strip() for n in names if n and n.strip()]
    return resolve_country_list(spec)
