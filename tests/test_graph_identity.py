"""Guards the modularization refactor: the compiled deep_researcher graph's node set must not
change as functions move into the nodes/ package. Update EXPECTED_NODES only for an intentional
graph change (not a move)."""
from open_deep_research.deep_researcher import deep_researcher

# Paste the exact sorted list printed in Step 1 between the brackets:
EXPECTED_NODES = {
    "__end__",
    "__start__",
    "answer_from_dossier",
    "answer_from_facts",
    "assess_completeness",
    "assess_knowledge",
    "assess_sufficiency",
    "clarify_with_user",
    "extract_facts",
    "final_report_generation",
    "persist_research",
    "preallocate_run",
    "research_supervisor",
    "resolve_required_qualifiers",
    "synthesize_narrative",
    "write_research_brief",
}


def test_graph_node_set_is_stable():
    nodes = set(deep_researcher.get_graph().nodes)
    assert nodes == EXPECTED_NODES, f"node set drift: +{nodes - EXPECTED_NODES} -{EXPECTED_NODES - nodes}"


def test_graph_compiles_and_has_entry():
    g = deep_researcher.get_graph()
    assert "__start__" in {str(n) for n in g.nodes}


def test_resolve_required_qualifiers_node_present():
    from open_deep_research.deep_researcher import deep_researcher as g
    assert "resolve_required_qualifiers" in g.get_graph().nodes
