"""Build and render a cross-instance comparison matrix from grouped fact rows.

Input rows are query.group_by_canonical() output (one row per instance/property/
canonical value). Output: rows = instances, columns = the profile's properties,
cell = canonical value(s) with a trailing '*' for trusted, '!' for in-conflict;
both markers can co-occur on the same value (e.g. ``launched*!`` means the value
is trusted AND disputed by another source); empty string = no fact (a visible
coverage gap).
"""
from __future__ import annotations

import csv
import io
from collections.abc import Callable


def _md_escape(s: str) -> str:
    """Escape pipe characters so a cell value can't break a markdown table."""
    return s.replace("|", r"\|")


def _cell_text(values: list[dict]) -> str:
    if not values:
        return ""
    parts = []
    for v in values:
        s = str(v.get("value", ""))
        if v.get("admission") == "trusted":
            s += "*"
        if v.get("in_conflict"):
            s += "!"
        parts.append(s)
    return "; ".join(sorted(parts))


def build_matrix(
    grouped_rows: list[dict],
    property_names: list[str],
    label: Callable[[str], str],
) -> list[dict]:
    """Build a comparison matrix from grouped fact rows.

    Args:
        grouped_rows: Rows from query.group_by_canonical(), each with keys
            instance_key, property_name, value, admission, in_conflict.
        property_names: Ordered list of property names to use as columns.
        label: Callable mapping instance_key to a display name.

    Returns:
        List of row dicts (sorted by display name) with keys instance_key,
        instance, and cells (a dict from property_name to cell text).
        Empty string cells indicate a coverage gap.
    """
    by_instance: dict[str, dict[str, list[dict]]] = {}
    for r in grouped_rows:
        ik = r.get("instance_key")
        if ik is None:
            continue  # pre-migration / unresolved rows have no instance — skip, don't crash the sort
        by_instance.setdefault(ik, {}).setdefault(r.get("property_name"), []).append(r)
    out = []
    for ik, props in by_instance.items():
        cells = {p: _cell_text(props.get(p, [])) for p in property_names}
        out.append({"instance_key": ik, "instance": label(ik), "cells": cells})
    out.sort(key=lambda row: row["instance"])
    return out


def render_matrix(
    grouped_rows: list[dict],
    property_names: list[str],
    label: Callable[[str], str],
    fmt: str = "text",
) -> str:
    """Render a comparison matrix as a string in the specified format.

    Args:
        grouped_rows: Rows from query.group_by_canonical().
        property_names: Ordered list of property names to use as columns.
        label: Callable mapping instance_key to a display name.
        fmt: Output format — one of 'text' (aligned columns), 'md' (Markdown
            table), or 'csv'.

    Returns:
        Formatted string representation of the matrix.
    """
    matrix = build_matrix(grouped_rows, property_names, label)
    headers = ["country", *property_names]
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(headers)
        for row in matrix:
            w.writerow([row["instance"], *[row["cells"][p] for p in property_names]])
        return buf.getvalue().rstrip("\n")
    if fmt == "md":
        lines = [
            "| " + " | ".join(_md_escape(h) for h in headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
        ]
        for row in matrix:
            lines.append(
                "| "
                + " | ".join(
                    _md_escape(v)
                    for v in [row["instance"], *[row["cells"][p] for p in property_names]]
                )
                + " |"
            )
        return "\n".join(lines)
    # text: aligned columns
    widths = [len(h) for h in headers]
    for row in matrix:
        widths[0] = max(widths[0], len(row["instance"]))
        for i, p in enumerate(property_names, start=1):
            widths[i] = max(widths[i], len(row["cells"][p]))

    def _align_row(cells: list[str]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    lines = [_align_row(headers)]
    for row in matrix:
        lines.append(_align_row([row["instance"], *[row["cells"][p] for p in property_names]]))
    return "\n".join(lines)
