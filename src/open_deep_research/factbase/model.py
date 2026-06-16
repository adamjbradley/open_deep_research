from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Fact:
    fact_id: int | None
    tuple_key: str
    as_of: int | None
    value: str
    unit: str | None
    source_meets_bar: bool
    has_unspecified_required: bool
    admission: str = "provisional"
    lifecycle: str = "current"
    canonical_value: str | None = None
    canonical_unit: str | None = None


@dataclass
class Promote:
    fact_id: int


@dataclass
class Demote:
    fact_id: int


@dataclass
class OpenConflict:
    tuple_key: str
    as_of: int | None
    fact_ids: list[int] = field(default_factory=list)


@dataclass
class AutoClose:
    tuple_key: str
    as_of: int | None


Intent = Promote | Demote | OpenConflict | AutoClose
