from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class PropertyDef:
    name: str
    value_kind: str
    description: str = ""
    identity_qualifiers: list[str] = field(default_factory=list)
    required_qualifiers: list[str] = field(default_factory=list)
    qualifier_enums: dict[str, list[str]] = field(default_factory=dict)
    value_enum: list[str] | None = None
    # Optional per-enum-value descriptions (value -> human description), surfaced in the
    # compiled extraction prompt. Empty unless the YAML uses the {value, description} form.
    value_enum_descriptions: dict[str, str] = field(default_factory=dict)
    trust_threshold: str = "reputable"
    # Maps a canonical value -> its surface variants (given as already-normalized lowercase
    # strings, i.e. as identity.canonical_value produces before alias-mapping). Used to
    # collapse semantically-equal values like "Aadhaar" / "Aadhaar Card" / "UID".
    value_aliases: dict[str, list[str]] = field(default_factory=dict)
    multi: bool = False
    open_world: bool = False
    narrative_required: bool = False
    narrative_guidance: str = ""
    completeness: str = "required"
    absence_allowed: bool = True

    def aliases_for(self, normalized_value: str) -> str | None:
        """Return the canonical value if ``normalized_value`` is a known variant, else None."""
        if not self.value_aliases:
            return None
        reverse = self.__dict__.get("_alias_reverse")
        if reverse is None:
            reverse = {}
            for canonical, variants in self.value_aliases.items():
                reverse[canonical.strip().lower()] = canonical.strip().lower()
                for variant in variants:
                    reverse[variant.strip().lower()] = canonical.strip().lower()
            self.__dict__["_alias_reverse"] = reverse
        return reverse.get((normalized_value or "").strip().lower())

    def validate(self, value: str) -> bool:
        v = (value or "").strip()
        if self.value_kind == "percentage":
            try:
                return 0.0 <= float(v.rstrip("%")) <= 100.0
            except ValueError:
                return False
        if self.value_kind == "number":
            s = v.replace(",", "").replace("_", "").replace(" ", "")
            try:
                return math.isfinite(float(s))  # reject inf/nan -- not valid counts
            except ValueError:
                return False
        if self.value_kind == "enum" and self.value_enum is not None:
            if self.multi:
                members = [m.strip().lower() for m in v.split(",") if m.strip()]
                if not members:
                    return True  # empty set == none captured
                if self.open_world:
                    return True  # any non-empty member set ok; unknowns kept verbatim
                allowed = {e.lower() for e in self.value_enum}
                return all(m in allowed for m in members)
            if self.open_world:
                return bool(v)  # single, open: any non-empty literal
            return v.lower() in {e.lower() for e in self.value_enum}
        return bool(v)


@dataclass
class Profile:
    entity_type: str
    properties: list[PropertyDef]
    overview_sections: list[str] = field(default_factory=list)

    def property(self, name: str) -> PropertyDef:
        for pd in self.properties:
            if pd.name == name:
                return pd
        raise KeyError(name)


def load(name: str) -> Profile:
    """Load a domain profile from its YAML data file (validated on load)."""
    import yaml
    from importlib.resources import files

    from .profile_schema import profile_from_dict

    text = (
        files("open_deep_research.factbase.profiles")
        .joinpath(f"{name}.yaml")
        .read_text(encoding="utf-8")
    )
    return profile_from_dict(yaml.safe_load(text))


def available_profiles() -> list[dict]:
    """List shipped domain profiles for query-driven selection.

    Returns one dict per loadable profile: ``{name, entity_type, notes, property_names}``.
    Skips source registries (no ``entity_type``) and ``*.draft.yaml`` proposals. ``name`` is
    the YAML stem (what ``load``/``profile_name`` expects). Best-effort: malformed files are
    skipped so a single bad file never breaks selection.
    """
    import yaml
    from importlib.resources import files

    out: list[dict] = []
    for entry in files("open_deep_research.factbase.profiles").iterdir():
        n = entry.name
        if not n.endswith(".yaml") or n.endswith(".draft.yaml"):
            continue
        try:
            data = yaml.safe_load(entry.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a malformed file shouldn't break selection
            continue
        if not (isinstance(data, dict) and data.get("entity_type")):
            continue  # registries have no entity_type
        out.append({
            "name": n[: -len(".yaml")],
            "entity_type": data.get("entity_type"),
            "notes": data.get("notes") or "",
            "property_names": [p["name"] for p in data.get("properties", []) if "name" in p],
        })
    out.sort(key=lambda p: p["name"])
    return out
