"""SQL construction guards can run without a PostgreSQL fixture."""

from importlib import import_module


def test_confirmed_tip_migration_preserves_session_and_information_ordering_sql():
    previous = import_module("migrations.versions.20260921_0030_current_price_information_time")
    current = import_module("migrations.versions.20260922_0034_current_price_confirmed_tips")
    before = previous._function(information_time_first=True)
    after = current._function()
    marker = "), daily_clocks AS MATERIALIZED ("
    assert after.split(marker, 1)[1] == before.split(marker, 1)[1]
    assert after.count("source_quote_tip AS MATERIALIZED") == 1
    assert "confirmation.confirmed_at" not in after
    assert after.count("confirmation.finished_at AS confirmed_at") == 2


def test_confirmed_tip_migration_seeks_only_after_confirmation_and_version_checks():
    current = import_module("migrations.versions.20260922_0034_current_price_confirmed_tips")
    tip = current._function().split("), confirmed_quote AS MATERIALIZED (", 1)[0]
    assert "fact.available_at <= p_as_of" in tip
    assert "price_run.status IN ('succeeded', 'partial')" in tip
    assert "price_run.finished_at <= p_as_of" in tip
    assert tip.index("AND NOT EXISTS") < tip.index("LIMIT 1")
    assert "source.kind NOT IN ('daily_bars', 'daily_quote')" in tip
    assert "fact_available_at = fact.available_at" in tip
