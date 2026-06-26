from __future__ import annotations

from . import model


def evaluate(fact: model.Fact, bucket: list[model.Fact], has_open_conflict: bool):
    eligible = (fact.source_meets_bar and not fact.has_unspecified_required
                and not has_open_conflict and not fact.has_inferred_required)
    if eligible and fact.admission != "trusted":
        return model.Promote(fact.fact_id)
    if not eligible and fact.admission == "trusted":
        return model.Demote(fact.fact_id)
    return None
