from open_deep_research import deep_researcher as dr


def test_graph_has_extract_facts_node_on_research_path():
    nodes = set(dr.deep_researcher.get_graph().nodes.keys())
    assert "extract_facts" in nodes
    assert "preallocate_run" in nodes


def test_extract_facts_noop_when_persist_disabled():
    import asyncio
    from langchain_core.runnables import RunnableConfig
    state = {"messages": [], "research_brief": "x"}
    cfg = RunnableConfig(configurable={"persist_results": False, "thread_id": "t-noop"})
    out = asyncio.run(dr.extract_facts(state, cfg))
    assert out == {} or out is None
