from importlib.resources import files

import yaml


def test_population_data_is_country_keyed_with_values():
    text = files("open_deep_research.factbase.data").joinpath("population.yaml").read_text(
        encoding="utf-8")
    data = yaml.safe_load(text)
    assert len(data) > 150                         # broad country coverage
    assert "IND" in data and "USA" in data
    ind = data["IND"]
    assert isinstance(ind["value"], int) and ind["value"] > 1_000_000_000
    assert isinstance(ind["year"], int) and 2000 <= ind["year"] <= 2100
    assert "WLD" not in data and "EUU" not in data  # no World Bank aggregates
