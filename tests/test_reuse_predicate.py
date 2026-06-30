from datetime import datetime, timezone
from open_deep_research.factbase.reuse import is_reusable

NOW = datetime(2026, 6, 30, tzinfo=timezone.utc)


def _row(**kw):
    base = {"in_conflict": False, "trusted_captured_at": "2026-06-01T00:00:00Z"}
    base.update(kw); return base


def test_trusted_recent_unconflicted_is_reusable():
    assert is_reusable(_row(), now=NOW, max_age_days=180) is True

def test_no_trusted_row_not_reusable():
    assert is_reusable(_row(trusted_captured_at=None), now=NOW, max_age_days=180) is False

def test_trusted_but_stale_not_reusable():
    assert is_reusable(_row(trusted_captured_at="2024-01-01T00:00:00Z"), now=NOW, max_age_days=180) is False

def test_in_conflict_not_reusable():
    assert is_reusable(_row(in_conflict=True), now=NOW, max_age_days=180) is False

def test_unparseable_timestamp_not_reusable():
    assert is_reusable(_row(trusted_captured_at="not-a-date"), now=NOW, max_age_days=180) is False

def test_future_timestamp_not_reusable():   # clock-skew lower bound (agy r12 Medium)
    assert is_reusable(_row(trusted_captured_at="2027-01-01T00:00:00Z"), now=NOW, max_age_days=180) is False


def test_property_reusable_any_good_none_conflict():   # agy r12 High: property-level over a LIST
    from open_deep_research.factbase.reuse import is_property_reusable
    rows = [_row(trusted_captured_at="2026-06-01T00:00:00Z"), _row(trusted_captured_at=None)]
    assert is_property_reusable(rows, now=NOW, max_age_days=180) is True

def test_property_not_reusable_if_any_row_conflicts():
    from open_deep_research.factbase.reuse import is_property_reusable
    rows = [_row(trusted_captured_at="2026-06-01T00:00:00Z"), _row(in_conflict=True)]
    assert is_property_reusable(rows, now=NOW, max_age_days=180) is False

def test_property_not_reusable_when_no_row_good():
    from open_deep_research.factbase.reuse import is_property_reusable
    rows = [_row(trusted_captured_at=None), _row(trusted_captured_at="2024-01-01T00:00:00Z")]
    assert is_property_reusable(rows, now=NOW, max_age_days=180) is False
