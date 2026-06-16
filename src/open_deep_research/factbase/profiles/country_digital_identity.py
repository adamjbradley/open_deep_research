from __future__ import annotations

from open_deep_research.factbase.profile import Profile, PropertyDef

PROFILE = Profile(
    entity_type="country",
    properties=[
        PropertyDef(
            "foundational_id_scheme",
            "name",
            # Variants are matched AFTER identity.canonical_value's text normalization
            # (lowercase, parentheticals/punctuation stripped, trailing "card"/"scheme"
            # removed -- so "Aadhaar Card" already collapses to "aadhaar" without an alias).
            # These catch India-specific phrasings the deterministic rules can't infer.
            value_aliases={
                "aadhaar": [
                    "uidai",
                    "aadhaar uid",
                    "uid aadhaar",
                    "unique identity scheme or aadhaar",
                    "unique identity uid scheme or aadhaar",
                ],
            },
        ),
        PropertyDef(
            "scheme_status",
            "enum",
            identity_qualifiers=["basis"],
            required_qualifiers=["basis"],
            qualifier_enums={"basis": ["de_jure", "de_facto"]},
            value_enum=["announced", "piloting", "operational", "mandatory"],
        ),
        PropertyDef(
            "id_coverage_pct",
            "percentage",
            identity_qualifiers=["population_basis", "coverage_kind", "measured_modeled"],
            required_qualifiers=["population_basis"],
            qualifier_enums={
                "population_basis": ["adults_15plus", "total_pop", "births", "registered_holders"],
                "coverage_kind": ["enrolled", "issued", "active"],
                "measured_modeled": ["measured", "modeled"],
            },
        ),
        PropertyDef(
            "biometric_capture",
            "enum",
            value_enum=["none", "photo", "fingerprint", "iris", "multi"],
        ),
        PropertyDef(
            "data_protection_law",
            "boolean",
            identity_qualifiers=["jurisdiction", "stage", "scope"],
            required_qualifiers=["stage"],
            qualifier_enums={
                "stage": ["enacted", "in_force"],
                "scope": ["comprehensive", "sectoral"],
            },
        ),
        PropertyDef("legal_basis", "name_year", identity_qualifiers=["jurisdiction"]),
    ],
)
