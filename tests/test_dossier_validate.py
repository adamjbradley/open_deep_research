from open_deep_research.factbase.dossier import validate_profiles


def test_validate_passes_on_real_profiles():
    report, ok = validate_profiles()
    assert ok is True
    assert "country_digital_identity" in report
    assert "di_source_registry" in report


def test_validate_fails_on_bad_profile(tmp_path):
    bad = tmp_path / "country_bad.yaml"
    bad.write_text("entity_type: country\nproperties:\n  - {name: x, kind: wat}\n", encoding="utf-8")
    report, ok = validate_profiles(extra_paths=[bad])
    assert ok is False
    assert "country_bad" in report and "unknown kind" in report
