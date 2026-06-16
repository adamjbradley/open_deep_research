"""Canonical rendering for the dossier surface. Never present provisional/contested as established."""
from __future__ import annotations
import csv
import io

_COLUMNS = ["instance_key", "property_name", "qualifiers", "as_of", "value",
            "source_url", "source_tier", "status"]
# Grouped view: one row per canonical value, with corroborating-source count + raw variants.
_GROUPED_COLUMNS = ["instance_key", "property_name", "qualifiers", "as_of", "value",
                    "sources", "variants", "status"]


def _status(row: dict) -> str:
    marks = []
    if row.get("in_conflict"):
        marks.append("⚠ in-conflict")
    if row.get("admission") != "trusted":
        marks.append("~prov")
    return " ".join(marks) if marks else "trusted"


def _cell(row: dict, col: str) -> str:
    if col == "status":
        return _status(row)
    if col == "qualifiers":
        return ";".join(f"{k}={v}" for k, v in (row.get("qualifiers") or {}).items())
    if col == "value":
        v = str(row.get("value", ""))
        u = row.get("unit") or ""
        return f"{v}{u}"
    if col == "sources":
        return str(row.get("source_count", ""))
    if col == "variants":
        return "; ".join(row.get("variants") or [])
    return "" if row.get(col) is None else str(row.get(col))


def render(rows: list[dict], fmt: str = "text") -> str:
    if not rows:
        return "No facts found."
    # Grouped rows (from query.group_by_canonical) carry a source_count; render the
    # canonical-value view with corroborating-source counts + variants.
    columns = _GROUPED_COLUMNS if rows and "source_count" in rows[0] else _COLUMNS
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(columns)
        for r in rows:
            w.writerow([_cell(r, c) for c in columns])
        return buf.getvalue()
    if fmt == "md":
        head = "| " + " | ".join(columns) + " |"
        sep = "| " + " | ".join("---" for _ in columns) + " |"
        body = ["| " + " | ".join(_cell(r, c) for c in columns) + " |" for r in rows]
        return "\n".join([head, sep, *body])
    # text: aligned columns
    table = [columns] + [[_cell(r, c) for c in columns] for r in rows]
    widths = [max(len(table[i][j]) for i in range(len(table))) for j in range(len(columns))]
    return "\n".join("  ".join(cell.ljust(widths[j]) for j, cell in enumerate(line)) for line in table)
