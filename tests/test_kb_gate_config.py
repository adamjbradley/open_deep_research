from open_deep_research.configuration import Configuration


def test_kb_gate_defaults():
    c = Configuration()
    assert c.kb_first_gate is False
    assert c.kb_reuse_max_age_days == 180

def test_kb_gate_from_config():
    c = Configuration.from_runnable_config(
        {"configurable": {"kb_first_gate": True, "kb_reuse_max_age_days": 30}})
    assert c.kb_first_gate is True and c.kb_reuse_max_age_days == 30
