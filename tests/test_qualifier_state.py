from open_deep_research.state import AgentState
from open_deep_research.configuration import Configuration


def test_state_has_qualifier_research_attempted():
    assert "qualifier_research_attempted" in AgentState.__annotations__


def test_config_has_max_qualifier_resolutions_default():
    c = Configuration()
    assert c.max_qualifier_resolutions == 12
