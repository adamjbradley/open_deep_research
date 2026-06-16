import asyncio

import pytest

from open_deep_research.factbase.country_list import resolve_country_list_async


def test_scout_returns_names_from_model():
    async def fake_scout(query):
        assert "CBDC" in query
        return ["Nigeria", "Bahamas", "Jamaica"]
    out = asyncio.run(resolve_country_list_async(
        spec=None, scout_query="countries with a launched CBDC programme", scout_call=fake_scout))
    # NOTE: the query passed to fake_scout must contain "CBDC" for the inner assert.
    assert out == ["Nigeria", "Bahamas", "Jamaica"]


def test_non_scout_delegates_to_sync():
    out = asyncio.run(resolve_country_list_async(spec="Nigeria, India", scout_query=None, scout_call=None))
    assert out == ["Nigeria", "India"]


def test_scout_without_call_raises():
    with pytest.raises(ValueError):
        asyncio.run(resolve_country_list_async(spec=None, scout_query="x", scout_call=None))
