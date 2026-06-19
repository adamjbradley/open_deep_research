"""Tests for batch.default_run_one configuration: whole-profile mode and summarization.

Tasks B1 and D1.
"""
from open_deep_research.configuration import Configuration
from open_deep_research.factbase.batch import _default_run_one_configurable


def test_batch_config_is_whole_profile(monkeypatch):
    """default_run_one must resolve whole_profile_mode=True, facts_first_mode=False."""
    for k in ("FACTS_FIRST_MODE", "WHOLE_PROFILE_MODE", "SUMMARIZE_SEARCH_RESULTS"):
        monkeypatch.delenv(k, raising=False)
    c = Configuration.from_runnable_config(
        {"configurable": _default_run_one_configurable("country_digital_identity", "research_results.db")}
    )
    assert c.whole_profile_mode is True
    assert c.facts_first_mode is False


def test_batch_disables_summarization(monkeypatch):
    """default_run_one must resolve summarize_search_results=False."""
    monkeypatch.delenv("SUMMARIZE_SEARCH_RESULTS", raising=False)
    c = Configuration.from_runnable_config(
        {"configurable": _default_run_one_configurable("country_digital_identity", "research_results.db")}
    )
    assert c.summarize_search_results is False
