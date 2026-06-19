"""Tests for batch.default_run_one configuration: whole-profile mode and summarization.

Tasks B1 and D1.
"""
from open_deep_research.configuration import Configuration


def _default_run_one_configurable(profile="country_digital_identity"):
    """Mirror batch.default_run_one's configurable dict (keep in sync)."""
    return {
        "profile_name": profile,
        "use_knowledge_base": False,
        "allow_clarification": False,
        "persist_results": True,
        "max_concurrent_research_units": 2,
        "max_researcher_iterations": 2,
        "whole_profile_mode": True,
        "summarize_search_results": False,
    }


def test_batch_config_is_whole_profile(monkeypatch):
    """default_run_one must resolve whole_profile_mode=True, facts_first_mode=False."""
    for k in ("FACTS_FIRST_MODE", "WHOLE_PROFILE_MODE", "SUMMARIZE_SEARCH_RESULTS"):
        monkeypatch.delenv(k, raising=False)
    c = Configuration.from_runnable_config({"configurable": _default_run_one_configurable()})
    assert c.whole_profile_mode is True
    assert c.facts_first_mode is False


def test_batch_disables_summarization(monkeypatch):
    """default_run_one must resolve summarize_search_results=False."""
    monkeypatch.delenv("SUMMARIZE_SEARCH_RESULTS", raising=False)
    c = Configuration.from_runnable_config({"configurable": _default_run_one_configurable()})
    assert c.summarize_search_results is False
