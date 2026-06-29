from __future__ import annotations

import json

from . import model


def has_inferred_required_qualifier(qualifier_provenance_json: str | None) -> bool:
    """Return True if any qualifier in the provenance JSON is marked `inferred`."""
    prov = json.loads(qualifier_provenance_json or "{}")
    return any(v == "inferred" for v in prov.values())


def evaluate(fact: model.Fact, bucket: list[model.Fact], has_open_conflict: bool):
    eligible = (fact.source_meets_bar and not fact.has_unspecified_required
                and not has_open_conflict and not fact.has_inferred_required)
    if eligible and fact.admission != "trusted":
        return model.Promote(fact.fact_id)
    if not eligible and fact.admission == "trusted":
        return model.Demote(fact.fact_id)
    return None
