"""Expand a CLI country-list spec into a list of country names.

Three input shapes (scout discovery lives in Plan 7b, where the model call is):
  - "@/path/to/file"   one country name per line
  - a known group name ("G20", "EU", "West Africa") -> its members
  - a comma-separated list of names ("A, B, C")
A single bare token that is not a known group is treated as one explicit name.
"""
from __future__ import annotations


def _load_groups() -> dict[str, list[str]]:
    import yaml
    from importlib.resources import files

    text = files("open_deep_research.factbase.data").joinpath("groups.yaml").read_text(
        encoding="utf-8")
    return yaml.safe_load(text) or {}


def resolve_country_list(spec: str) -> list[str]:
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("empty country-list spec")
    if spec.startswith("@"):
        with open(spec[1:], encoding="utf-8") as fh:
            names = [ln.strip() for ln in fh]
        out = [n for n in names if n]
        if not out:
            raise ValueError(f"no country names in file {spec[1:]}")
        return out
    if "," not in spec:
        groups = _load_groups()
        if spec in groups:
            return list(groups[spec])
        return [spec]  # a single explicit name
    return [part.strip() for part in spec.split(",") if part.strip()]
