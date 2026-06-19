"""Pydantic meta-schema for domain profiles, plus a builder to the runtime dataclasses.

Validates a parsed YAML profile (structure, enums, qualifier coherence, alias
integrity) and constructs the existing ``Profile``/``PropertyDef`` dataclasses.
Kept separate from ``profile.py`` to avoid an import cycle (loaded lazily there).
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional, Union

from pydantic import BaseModel, model_validator

from .profile import Profile, PropertyDef

_VALID_KINDS = {"name", "enum", "percentage", "boolean", "name_year", "number", "text"}


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
    multi: bool = False
    open: bool = False
    narrative: Optional[dict] = None
    completeness: str = "required"
    absence_allowed: bool = True

    @model_validator(mode="after")
    def _check(self) -> "PropertyModel":
        if self.kind not in _VALID_KINDS:
            raise ValueError(f"property {self.name!r}: unknown kind {self.kind!r}")
        if self.value_enum is not None and self.kind != "enum":
            raise ValueError(f"property {self.name!r}: value_enum only allowed for kind 'enum'")
        if self.multi and self.kind != "enum":
            raise ValueError(f"property {self.name!r}: multi only allowed for kind 'enum'")
        if self.open and self.kind != "enum":
            raise ValueError(f"property {self.name!r}: open only allowed for kind 'enum'")
        if (self.multi or self.open) and self.value_enum is None:
            raise ValueError(f"property {self.name!r}: multi/open requires value_enum")
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
        if self.completeness not in ("required", "optional"):
            raise ValueError(f"property {self.name!r}: completeness must be 'required' or 'optional'")
        if self.narrative is not None and not isinstance(self.narrative, dict):
            raise ValueError(f"property {self.name!r}: narrative must be a mapping")
        return self

    def enum_values(self) -> Optional[list[str]]:
        if self.value_enum is None:
            return None
        return [e.value if isinstance(e, _EnumValue) else e for e in self.value_enum]

    def enum_descriptions(self) -> dict[str, str]:
        return {
            e.value: e.description
            for e in (self.value_enum or [])
            if isinstance(e, _EnumValue) and e.description
        }


class ProfileModel(BaseModel):
    entity_type: str
    version: str = "1"
    notes: Optional[str] = None
    narrative: Optional[dict] = None
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
            description=p.description or "",
            identity_qualifiers=list(p.identity_qualifiers),
            required_qualifiers=list(p.required_qualifiers),
            qualifier_enums={k: list(v) for k, v in p.qualifier_enums.items()},
            value_enum=p.enum_values(),
            value_enum_descriptions=p.enum_descriptions(),
            trust_threshold=p.trust_threshold,
            value_aliases={k: list(v) for k, v in p.value_aliases.items()},
            multi=p.multi,
            open_world=p.open,
            narrative_required=bool((p.narrative or {}).get("required", False)),
            narrative_guidance=str((p.narrative or {}).get("guidance", "") or ""),
            completeness=p.completeness,
            absence_allowed=p.absence_allowed,
        )
        for p in model.properties
    ]
    prof = Profile(
        entity_type=model.entity_type,
        properties=props,
        overview_sections=list((data.get("narrative") or {}).get("overview_sections", []) or []),
    )
    prof.profile_version = model.version
    # Hash the SEMANTIC profile (validated, normalized) — NOT raw file bytes — so inert
    # comments, `description`/`notes`, and formatting churn don't trigger false drift.
    semantic = {
        "entity_type": model.entity_type,
        "properties": [
            {
                "name": pd.name,
                "kind": pd.value_kind,
                "identity_qualifiers": sorted(pd.identity_qualifiers),
                "required_qualifiers": sorted(pd.required_qualifiers),
                "qualifier_enums": {k: sorted(v) for k, v in pd.qualifier_enums.items()},
                "value_enum": None if pd.value_enum is None else sorted(pd.value_enum),
                "trust_threshold": pd.trust_threshold,
                "value_aliases": {k: sorted(v) for k, v in pd.value_aliases.items()},
                "multi": pd.multi,
                "open": pd.open_world,
            }
            for pd in sorted(props, key=lambda p: p.name)
        ],
    }
    prof.profile_hash = hashlib.sha256(
        json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return prof
