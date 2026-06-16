from importlib.resources import files

import pytest


def test_iso3166_is_importable_resource():
    pkg = files("open_deep_research.factbase.data")
    iso = pkg.joinpath("iso3166.yaml").read_text(encoding="utf-8")
    assert "BHS" in iso and "NGA" in iso  # blocker cases covered (Bahamas, Nigeria)


def test_groups_resource_present_once_created():
    pkg = files("open_deep_research.factbase.data")
    if not pkg.joinpath("groups.yaml").is_file():
        pytest.skip("groups.yaml is created in Task 3")
    assert "G20" in pkg.joinpath("groups.yaml").read_text(encoding="utf-8")
