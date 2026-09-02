from datetime import UTC, datetime

from investment_panel.core.phase2 import Phase2Status
from investment_panel.jobs.update_phase2_sources import adapt_source_payload


def test_mock_authorized_adapter_persists_explicit_source_clock_only() -> None:
    now = datetime(2026, 9, 2, 14, tzinfo=UTC).isoformat()
    result = adapt_source_payload(
        "fred",
        {"observations": [{"series_id": "GDP", "date": "2026-09-01", "available_at": now, "value": "1.2"}]},
        env={"FRED_API_KEY": "configured-for-test"},
    )
    assert result.status is Phase2Status.AVAILABLE
    assert result.observations[0].available_at.isoformat() == now
    assert "configured-for-test" not in result.observations[0].model_dump_json()


def test_mock_authorized_adapter_marks_absent_clock_as_missing_history() -> None:
    result = adapt_source_payload(
        "fred",
        {"observations": [{"series_id": "GDP", "date": "2026-09-01", "value": "1.2", "vintage_at": "2026-09-01"}]},
        env={"FRED_API_KEY": "configured-for-test"},
    )
    assert result.status is Phase2Status.MISSING_HISTORY
    assert result.observations == ()


def test_existing_sec_and_option_seams_are_explicitly_dispatched() -> None:
    now = "2026-09-02T14:00:00+00:00"
    options = adapt_source_payload("ibkr_options", {"observations": [
        {"contract_id": "c1", "observed_at": now, "available_at": now, "open_interest": 10, "volume": 2},
    ]})
    assert options.status is Phase2Status.AVAILABLE
    assert {item.field_name for item in options.observations} == {"option.open_interest", "option.volume"}
    positioning = adapt_source_payload("sec_13f", {"observations": [
        {"issuer": "issuer-1", "filing_date": now, "observed_at": now, "available_at": now, "shares": 100},
    ]})
    assert positioning.status is Phase2Status.AVAILABLE
    assert positioning.observations[0].field_name == "positioning.flow"
    assert adapt_source_payload("robinhood_history_full", {}).status is Phase2Status.MISSING_HISTORY
