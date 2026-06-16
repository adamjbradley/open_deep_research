from __future__ import annotations

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
    # Maps a canonical value -> its surface variants (given as already-normalized lowercase
    # strings, i.e. as identity.canonical_value produces before alias-mapping). Used to
    # collapse semantically-equal values like "Aadhaar" / "Aadhaar Card" / "UID".
    value_aliases: dict[str, list[str]] = field(default_factory=dict)

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
