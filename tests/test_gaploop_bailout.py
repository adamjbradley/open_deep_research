import asyncio

from langchain_core.messages import HumanMessage

from open_deep_research import deep_researcher as dr
from open_deep_research.deep_researcher import _gaploop_decision
from open_deep_research.nodes import brief


def test_no_progress_gap_round_bails():
    # a gap round (rounds_used=1) whose incomplete set is unchanged -> finalize
    goto, no_progress = _gaploop_decision(["data_protection_law"], ["data_protection_law"], 1, 6)
    assert goto == "synthesize_narrative"
    assert no_progress is True


def test_progress_gap_round_continues():
    # incomplete shrank (a gap closed) -> another gap round
    goto, no_progress = _gaploop_decision(["data_protection_law"], ["data_protection_law", "biometric_capture"], 1, 6)
    assert goto == "write_research_brief"
    assert no_progress is False


def test_first_assessment_never_bails():
    # rounds_used=0, no prev -> gap round even though incomplete (can't be "no progress" yet)
    goto, no_progress = _gaploop_decision(["x", "y"], None, 0, 6)
    assert goto == "write_research_brief"
    assert no_progress is False


def test_budget_exhausted_finalizes():
    # rounds_used+1 == max_rounds -> finalize via budget (not the bail flag)
    goto, no_progress = _gaploop_decision(["x"], ["x", "y"], 5, 6)
    assert goto == "synthesize_narrative"
    assert no_progress is False


def test_all_complete_finalizes():
    # nothing incomplete -> finalize regardless
    goto, _ = _gaploop_decision([], ["x"], 1, 6)
    assert goto == "synthesize_narrative"


def test_gap_round_brief_is_scoped_when_dossier_exists(monkeypatch):
    # Guards the assumption that partial-persist (#45) makes a dossier exist each round, so
    # write_research_brief takes its gap-scoped branch (not the whole-profile "comprehensive" brief).
    async def fake_get_subject(db_path, slug):
        return {"name": "Estonia",
                "current_report": "## Prior dossier\n- foundational_id_scheme: ID card",
                "sources": []}
    monkeypatch.setattr(brief, "get_subject_by_slug", fake_get_subject)

    state = {
        "messages": [HumanMessage(content="Research Estonia's digital identity")],
        "subject": "Estonia",
        "missing_information": "data_protection_law (missing_value)",
        "target_properties": ["data_protection_law"],
    }
    cfg = {"configurable": {"whole_profile_mode": True, "database_path": "/tmp/gaploop_brief.db",
                            "profile_name": "country_digital_identity", "thread_id": "t"}}
    result = asyncio.run(dr.write_research_brief(state, cfg))
    research_brief = result["research_brief"]
    # gap-scoped branch fired: focus on the missing info + cite the prior dossier
    assert "currently missing" in research_brief.lower()
    assert "data_protection_law" in research_brief
    assert "Prior dossier" in research_brief


def test_whole_profile_gap_brief_demotes_dossier_to_reference(monkeypatch):
    # W2: in a whole-profile gap round the missing properties are the PRIMARY objective and the
    # prior dossier is reference-only. The brief must not instruct re-verifying/re-gathering the
    # whole dossier (which crowds out the actual gaps under a bounded research budget).
    async def fake_get_subject(db_path, slug):
        return {"name": "Estonia",
                "current_report": "## Prior dossier\n- foundational_id_scheme: ID card",
                "sources": []}
    monkeypatch.setattr(brief, "get_subject_by_slug", fake_get_subject)
    state = {
        "messages": [HumanMessage(content="Research Estonia's digital identity")],
        "subject": "Estonia",
        "missing_information": "data_protection_law (missing_value)",
        "target_properties": ["data_protection_law"],
    }
    cfg = {"configurable": {"whole_profile_mode": True, "database_path": "/tmp/gaploop_brief_w2.db",
                            "profile_name": "country_digital_identity", "thread_id": "t"}}
    brief_text = asyncio.run(dr.write_research_brief(state, cfg))["research_brief"]
    low = brief_text.lower()
    assert "data_protection_law" in brief_text          # the gap is the objective
    assert "Prior dossier" in brief_text                # dossier still present, as context
    assert "verify the existing facts" not in low       # NOT the re-verify-everything framing
    assert "do not" in low and "reference" in low       # dossier demoted to reference-only
