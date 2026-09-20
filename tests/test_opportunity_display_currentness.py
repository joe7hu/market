"""Current presentation must not rewrite the historical trade-plan authority."""
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from investment_panel.application.read_models.loaders import opportunity_surface_state, plan_currentness_blocker

NOW = datetime(2026, 9, 20, 15, tzinfo=UTC)


def plan(**changes):
    return SimpleNamespace(eligibility='ACTIONABLE', cutoff=NOW-timedelta(days=2),
                           expiry=changes.pop('expiry', date(2026, 9, 25)), authorization_mode='PAPER', **changes)


def test_published_unexpired_terms_remain_researchable_during_weekend():
    assert opportunity_surface_state({}, plan(), now=NOW) == 'paper_review'


def test_expired_terms_do_not_look_paper_qualified():
    published = plan(expiry=date(2026, 9, 18))
    assert plan_currentness_blocker(published, now=NOW) == 'trade_plan_expired'
    assert opportunity_surface_state({}, published, now=NOW) == 'blocked'
    assert published.eligibility == 'ACTIONABLE'  # stored publication is untouched


def test_future_publication_cutoff_is_not_current_evidence():
    published = plan()
    published.cutoff = NOW+timedelta(seconds=1)
    assert plan_currentness_blocker(published, now=NOW) == 'trade_plan_cutoff_in_future'


def test_missing_research_plan_is_not_a_market_closed_failure():
    assert opportunity_surface_state({}, None, now=NOW) == 'research'
