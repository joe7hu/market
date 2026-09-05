from datetime import UTC, datetime

from investment_panel.core.phase2 import Phase2Status
from investment_panel.jobs.update_phase2_sources import adapt_source_payload, payload_for


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


def test_fred_requests_each_series_and_maps_provider_vintage_clock() -> None:
    calls: list[str] = []

    def fetcher(_url, _headers, params):
        calls.append(params["series_id"])
        return {"realtime_start": "2026-09-02", "observations": [{"date": "2026-09-01", "value": "1.2"}]}

    result = payload_for("fred", fetcher=fetcher)
    assert calls == ["GDP", "CPIAUCSL", "UNRATE"]
    assert result["observations"][0]["available_at"] == "2026-09-02T00:00:00+00:00"


def test_treasury_xml_is_normalized_at_provider_boundary() -> None:
    payload = {
        "xml": """<feed xmlns:d="urn:test"><entry><content><d:properties>
          <d:NEW_DATE>2026-09-02</d:NEW_DATE><d:BC_5YEAR>4.0</d:BC_5YEAR><d:BC_10YEAR>4.0</d:BC_10YEAR>
          <d:TC_10YEAR>1.7</d:TC_10YEAR>
        </d:properties></content></entry></feed>""",
        "retrieved_at": "2026-09-02T14:00:00+00:00",
    }
    result = adapt_source_payload("treasury", payload)
    assert result.status is Phase2Status.AVAILABLE
    assert {(item.field_name, item.value) for item in result.observations} == {
        ("rates.nominal_yield", 4.0), ("rates.real_yield", 1.7),
    }
    assert len(result.observations) == 3
    assert len({item.observation_id for item in result.observations}) == 3


def test_treasury_payload_requests_nominal_and_real_documented_feeds(monkeypatch) -> None:
    calls: list[str] = []

    def fetcher(_url, _headers, params):
        calls.append(params["data"])
        code = "BC_10YEAR" if params["data"] == "daily_treasury_yield_curve" else "TC_10YEAR"
        return {
            "xml": f"<feed><entry><properties><NEW_DATE>2026-09-02T00:00:00</NEW_DATE><{code}>4.0</{code}></properties></entry></feed>",
            "retrieved_at": "2026-09-02T14:00:00+00:00",
        }

    monkeypatch.setenv("MARKET_TREASURY_YEAR", "2026")
    payload = payload_for("treasury", fetcher=fetcher)
    assert calls == ["daily_treasury_yield_curve", "daily_treasury_real_yield_curve"]
    assert len(payload["observations"]) == 2


def test_alphavantage_earnings_shape_is_normalized_to_existing_contract() -> None:
    result = payload_for(
        "alphavantage",
        fetcher=lambda _url, _headers, _params: {
            "symbol": "SPY",
            "quarterlyEarnings": [{
                "fiscalDateEnding": "2026-06-30",
                "reportedDate": "2026-07-30",
                "estimatedEPS": "1.25",
            }],
        },
    )
    adapted = adapt_source_payload("alphavantage", result, env={"ALPHAVANTAGE_API_KEY": "configured"})
    assert adapted.status is Phase2Status.AVAILABLE
    assert adapted.observations[0].value == 1.25
    assert adapted.observations[0].publication_at.isoformat() == "2026-07-30T00:00:00+00:00"
