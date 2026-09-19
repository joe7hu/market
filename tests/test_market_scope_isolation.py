"""Optional model failure must not erase the published broad-market baseline."""
from datetime import UTC, datetime

from investment_panel.application.read_models import loaders
from investment_panel.application.read_models.types import PanelData, DataStatus
from investment_panel.domain.market import publication
from investment_panel.settings import AppConfig


def test_optional_read_failure_preserves_core_tables_and_reports_its_owner(monkeypatch):
    calls = []
    def load(config, table_names, **kwargs):
        calls.append(tuple(table_names))
        core = "market_environment_assets" in table_names
        return PanelData(status=DataStatus(core, "available" if core else "read failure", "postgresql"),
            tables={name: ([{"symbol": "SPY", "price": 100}] if name == "market_environment_assets" else []) for name in table_names},
            metadata={"table_counts": {name: (1 if name == "market_environment_assets" else 0) for name in table_names}})
    monkeypatch.setattr(loaders, "load_panel_data", load)
    result = loaders.load_panel_scope_data(AppConfig(), "market")
    assert result.status.ready
    assert result.rows("market_environment_assets")[0]["symbol"] == "SPY"
    assert result.metadata["optional_models_failed"] is True
    assert result.metadata["market_model_status"]["market_state_posterior"]["state"] == "failed"
    assert result.metadata["market_model_status"]["market_environment_assets"]["state"] == "available"
    assert len(calls) == 2


def test_optional_computation_failure_still_produces_baseline_snapshot(monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError("advanced model unavailable")
    monkeypatch.setattr(publication, "build_market_state_posterior", fail)
    result = publication.build_market_publication(as_of=datetime.now(UTC), inputs={
        "instrument_rows": [], "bars_by_id": {}, "price_rows": [], "valuation_rows": [],
        "event_risk_evidence": {}, "corporate_cycle_evidence": {}, "crypto_volume_evidence": {},
        "phase2_rows": [], "phase2_source_rows": [],
    })
    assert result["snapshot"].snapshot_id
    assert len(result["coverage_rows"]) == 48
    assert result["optional_errors"] == {"advanced_model": "model_unavailable"}
    assert result["phase2_posterior"] is None
