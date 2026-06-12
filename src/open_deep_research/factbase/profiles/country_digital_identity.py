from __future__ import annotations

from open_deep_research.factbase.profile import Profile, PropertyDef

PROFILE = Profile(
    entity_type="country",
    properties=[
        PropertyDef("foundational_id_scheme", "name"),
        PropertyDef(
            "scheme_status",
            "enum",
            identity_qualifiers=["basis"],
            qualifier_enums={"basis": ["de_jure", "de_facto"]},
            value_enum=["announced", "piloting", "operational", "mandatory"],
        ),
        PropertyDef(
            "id_coverage_pct",
            "percentage",
            identity_qualifiers=["population_basis", "coverage_kind", "measured_modeled"],
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
            qualifier_enums={
                "stage": ["enacted", "in_force"],
                "scope": ["comprehensive", "sectoral"],
            },
        ),
        PropertyDef("legal_basis", "name_year", identity_qualifiers=["jurisdiction"]),
    ],
)
