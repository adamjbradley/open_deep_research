"""Pydantic meta-schema for domain profiles, plus a builder to the runtime dataclasses.

Validates a parsed YAML profile (structure, enums, qualifier coherence, alias
integrity) and constructs the existing ``Profile``/``PropertyDef`` dataclasses.
Kept separate from ``profile.py`` to avoid an import cycle (loaded lazily there).
"""
from __future__ import annotations

from typing import Optional, Union

from pydantic import BaseModel, model_validator

from .profile import Profile, PropertyDef

_VALID_KINDS = {"name", "enum", "percentage", "boolean", "name_year"}


class _EnumValue(BaseModel):
    value: str
    description: Optional[str] = None


class PropertyModel(BaseModel):
    name: str
    kind: str
    description: Optional[str] = None
    identity_qualifiers: list[str] = []
    required_qualifiers: list[str] = []
    qualifier_enums: dict[str, list[str]] = {}
    value_enum: Optional[list[Union[str, _EnumValue]]] = None
    trust_threshold: str = "reputable"
    value_aliases: dict[str, list[str]] = {}

    @model_validator(mode="after")
    def _check(self) -> "PropertyModel":
        if self.kind not in _VALID_KINDS:
            raise ValueError(f"property {self.name!r}: unknown kind {self.kind!r}")
        if self.value_enum is not None and self.kind != "enum":
            raise ValueError(f"property {self.name!r}: value_enum only allowed for kind 'enum'")
        missing = set(self.required_qualifiers) - set(self.identity_qualifiers)
        if missing:
            raise ValueError(
                f"property {self.name!r}: required_qualifiers {sorted(missing)} not in identity_qualifiers"
            )
        known = set(self.identity_qualifiers) | set(self.required_qualifiers)
        undeclared = set(self.qualifier_enums) - known
        if undeclared:
            raise ValueError(
                f"property {self.name!r}: qualifier_enums keys {sorted(undeclared)} are not declared qualifiers"
            )
        seen: dict[str, str] = {}
        for canonical, variants in self.value_aliases.items():
            for surface in [canonical, *variants]:
                key = surface.strip().lower()
                if key in seen and seen[key] != canonical:
                    raise ValueError(
                        f"property {self.name!r}: alias {surface!r} maps to multiple canonicals"
                    )
                seen[key] = canonical
        return self

    def enum_values(self) -> Optional[list[str]]:
        if self.value_enum is None:
            return None
        return [e.value if isinstance(e, _EnumValue) else e for e in self.value_enum]


class ProfileModel(BaseModel):
    entity_type: str
    version: str = "1"
    notes: Optional[str] = None
    properties: list[PropertyModel]

    @model_validator(mode="after")
    def _check(self) -> "ProfileModel":
        if not self.entity_type.strip():
            raise ValueError("entity_type must be non-empty")
        names = [p.name for p in self.properties]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate property names: {dupes}")
        return self


def profile_from_dict(data: dict) -> Profile:
    """Validate a parsed profile dict and build the runtime ``Profile`` dataclass."""
    model = ProfileModel.model_validate(data)
    props = [
        PropertyDef(
            name=p.name,
            value_kind=p.kind,
            identity_qualifiers=list(p.identity_qualifiers),
            required_qualifiers=list(p.required_qualifiers),
            qualifier_enums={k: list(v) for k, v in p.qualifier_enums.items()},
            value_enum=p.enum_values(),
            trust_threshold=p.trust_threshold,
            value_aliases={k: list(v) for k, v in p.value_aliases.items()},
        )
        for p in model.properties
    ]
    prof = Profile(entity_type=model.entity_type, properties=props)
    prof.profile_version = model.version  # carried as an attribute; hash arrives in Phase 2
    return prof
