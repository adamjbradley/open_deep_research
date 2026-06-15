from __future__ import annotations

from . import identity, model


def detect(bucket: list[model.Fact], had_open_conflict: bool = False) -> list:
    """Pure conflict detection over a (tuple_key) bucket. Returns intents.

    Facts are reasoned about within their own ``as_of`` group: a conflict is
    only opened when two or more trust-bar facts sharing the same ``as_of``
    carry distinct canonicalized values. Facts below the trust bar never open
    a conflict. If a previously-open conflict collapses to at most one distinct
    value, an AutoClose is emitted.
    """
    if not bucket:
        return []

    trust_bar = [f for f in bucket if f.source_meets_bar]

    groups: dict[int | None, list[model.Fact]] = {}
    for f in trust_bar:
        groups.setdefault(f.as_of, []).append(f)

    intents: list = []
    any_conflict = False
    for as_of, facts in groups.items():
        distinct = {identity.canonicalize(f.value, f.unit) for f in facts}
        if len(distinct) >= 2:
            any_conflict = True
            intents.append(model.OpenConflict(
                tuple_key=facts[0].tuple_key,
                as_of=as_of,
                fact_ids=sorted(f.fact_id for f in facts if f.fact_id is not None),
            ))

    if not any_conflict and had_open_conflict:
        intents.append(model.AutoClose(tuple_key=bucket[0].tuple_key, as_of=bucket[0].as_of))

    return intents
