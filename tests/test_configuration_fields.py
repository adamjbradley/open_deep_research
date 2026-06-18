"""Test new configuration fields: whole_profile_mode and max_profile_rounds."""

from open_deep_research.configuration import Configuration


def test_whole_profile_defaults():
    c = Configuration()
    assert c.whole_profile_mode is False
    assert c.max_profile_rounds == 6
