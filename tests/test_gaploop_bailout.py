from open_deep_research.deep_researcher import _gaploop_decision


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
