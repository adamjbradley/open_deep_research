from __future__ import annotations

import importlib
from dataclasses import dataclass, field


@dataclass
class PropertyDef:
    name: str
    value_kind: str
    identity_qualifiers: list[str] = field(default_factory=list)
    required_qualifiers: list[str] = field(default_factory=list)
    qualifier_enums: dict[str, list[str]] = field(default_factory=dict)
    value_enum: list[str] | None = None
    trust_threshold: str = "reputable"

    def validate(self, value: str) -> bool:
        v = (value or "").strip()
        if self.value_kind == "percentage":
            try:
                return 0.0 <= float(v.rstrip("%")) <= 100.0
            except ValueError:
                return False
        if self.value_kind == "enum" and self.value_enum is not None:
            return v.lower() in {e.lower() for e in self.value_enum}
        return bool(v)


@dataclass
class Profile:
    entity_type: str
    properties: list[PropertyDef]

    def property(self, name: str) -> PropertyDef:
        for pd in self.properties:
            if pd.name == name:
                return pd
        raise KeyError(name)


def load(name: str) -> Profile:
    mod = importlib.import_module(f"open_deep_research.factbase.profiles.{name}")
    return mod.PROFILE
