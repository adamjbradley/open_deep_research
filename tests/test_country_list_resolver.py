import pytest

from open_deep_research.factbase.country_list import resolve_country_list


def test_explicit_comma_list():
    assert resolve_country_list("Nigeria, India ,Bahamas") == ["Nigeria", "India", "Bahamas"]


def test_at_file(tmp_path):
    p = tmp_path / "list.txt"
    p.write_text("Nigeria\nIndia\n\n  Bahamas  \n", encoding="utf-8")
    assert resolve_country_list(f"@{p}") == ["Nigeria", "India", "Bahamas"]


def test_named_group_expands():
    out = resolve_country_list("G20")
    assert "China" in out and "India" in out and len(out) == 19


def test_unknown_group_treated_as_single_name():
    # A bare token that is not a known group is treated as one explicit country name.
    assert resolve_country_list("Atlantis") == ["Atlantis"]


def test_empty_raises():
    with pytest.raises(ValueError):
        resolve_country_list("   ")
