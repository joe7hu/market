"""SQL loader metadata must not erase required source lineage during decoding."""
from datetime import UTC, datetime
from investment_panel.domain.market import publication
from investment_panel.domain.market.phase2 import EventObservation


def source_row(**updates):
    return dict(observation_id="treasury-yield-1", field_name="rates.nominal_yield", dimension="rates",
        asset_class="equity", source_id="treasury", source_version="v1", value=4.2,
        observed_at=datetime(2026, 9, 18, 20, tzinfo=UTC), available_at=datetime(2026, 9, 18, 20, tzinfo=UTC),
        status="AVAILABLE", source_enabled=True, source_operational_state="active", recent_rank=1,
        ingest_status="succeeded", ingest_finished_at=datetime(2026, 9, 18, 20, tzinfo=UTC),
        actual=None, consensus=None, surprise=None, revision=None, **updates)


def decode_through_publication(monkeypatch, row):
    captured = []
    original = publication.build_market_state_posterior
    def capture(observations, **kwargs):
        captured.extend(observations)
        return original(observations, **kwargs)
    monkeypatch.setattr(publication, "build_market_state_posterior", capture)
    publication.build_market_publication(
        as_of=datetime(2026, 9, 18, 20, 1, tzinfo=UTC),
        inputs={"instrument_rows": [], "bars_by_id": {}, "price_rows": [], "valuation_rows": [],
                "event_risk_evidence": {}, "corporate_cycle_evidence": {}, "crypto_volume_evidence": {},
                "phase2_rows": [row], "phase2_source_rows": []},
    )
    return captured


def test_required_source_identity_survives_loader_metadata_and_null_event_columns(monkeypatch):
    decoded = decode_through_publication(monkeypatch, source_row())
    assert len(decoded) == 1
    assert decoded[0].source_id == "treasury" and decoded[0].source_version == "v1"
    assert decoded[0].value == 4.2


def test_event_fields_are_preserved_not_mistaken_for_loader_metadata(monkeypatch):
    row = source_row()
    row.update(actual=4.2, consensus=4.0, surprise=.2)
    decoded = decode_through_publication(monkeypatch, row)
    assert len(decoded) == 1 and isinstance(decoded[0], EventObservation)
    assert decoded[0].consensus == 4.0 and decoded[0].surprise == .2


def test_missing_source_identity_is_not_invented(monkeypatch):
    row = source_row()
    del row["source_id"]
    assert not decode_through_publication(monkeypatch, row)


def test_zero_valuation_reference_remains_zero_without_inventing_neutral_posture():
    draft = publication.build_market_publication(as_of=datetime(2026, 9, 18, 20, 1, tzinfo=UTC), inputs={
        "instrument_rows": [], "bars_by_id": {}, "price_rows": [],
        "valuation_rows": [{"symbol": "SPY", "values": {"metric": "equity_risk_premium", "latest_value": 0, "value": 9}}],
        "event_risk_evidence": {}, "corporate_cycle_evidence": {}, "crypto_volume_evidence": {},
        "phase2_rows": [], "phase2_source_rows": [],
    })
    reference = draft["references"][0]
    assert reference["latest_value"] == 0
    assert reference["posture"] is None
