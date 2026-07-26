from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from investment_panel.database.authority import runtime_for_config
from investment_panel.database.instruments import reconcile_instrument
from investment_panel.database.thesis import thesis_history, thesis_monitor_rows
from investment_panel.jobs import run_thesis_monitor
from investment_panel.jobs.openai_option_agent import OpenAIOptionAgentError

_TEST_SYMBOLS = {"THIN", "HALL", "PRES", "DBNC"}


@pytest.fixture(autouse=True)
def _cleanup_thesis_automation_rows(migrated_postgres_dsn: str):
    _cleanup_symbols(migrated_postgres_dsn)
    yield
    _cleanup_symbols(migrated_postgres_dsn)


def test_thesis_automation_low_evidence_activation(migrated_postgres_dsn: str, tmp_path: Path, monkeypatch) -> None:
    _watch(migrated_postgres_dsn, "THIN")
    cfg = _config(tmp_path, migrated_postgres_dsn)

    monkeypatch.setattr(run_thesis_monitor, "generate_openai_thesis_monitor", lambda _request: _model_output("THIN"))

    result = run_thesis_monitor.run(str(cfg), force=True)
    rows = thesis_monitor_rows({"database": {"url": migrated_postgres_dsn}})

    assert result["completed"] == 1
    assert rows[0]["symbol"] == "THIN"
    assert rows[0]["confidence"] == "low"
    assert rows[0]["evidence_coverage_status"] == "low"
    assert rows[0]["author_kind"] == "ai"


def test_thesis_automation_rejects_hallucinated_evidence(migrated_postgres_dsn: str, tmp_path: Path, monkeypatch) -> None:
    _watch(migrated_postgres_dsn, "HALL")
    cfg = _config(tmp_path, migrated_postgres_dsn)

    def fake_model(_request):
        output = _model_output("HALL")
        output["evidence_assessments"] = [{
            "evidence_reference": "https://fabricated.example/hall",
            "evidence_title": "Fabricated",
            "evidence_date": None,
            "stance": "support",
            "materiality": "high",
            "affected_pillar_ids": ["p1"],
            "confidence": 0.9,
            "rationale": "Not in stored evidence.",
        }]
        return output

    monkeypatch.setattr(run_thesis_monitor, "generate_openai_thesis_monitor", fake_model)

    result = run_thesis_monitor.run(str(cfg), force=True)
    history = thesis_history({"database": {"url": migrated_postgres_dsn}}, "HALL")

    assert result["failed"] == 1
    assert "hallucinated evidence reference" in result["errors"][0]
    assert history["revisions"] == []
    with psycopg.connect(migrated_postgres_dsn) as connection:
        alert = connection.execute("SELECT alert_type FROM app.alert WHERE alert_type = 'thesis_automation_health'").fetchone()
    assert alert is not None


def test_thesis_automation_failure_preserves_previous_revision(migrated_postgres_dsn: str, tmp_path: Path, monkeypatch) -> None:
    _watch(migrated_postgres_dsn, "PRES")
    config = {"database": {"url": migrated_postgres_dsn}}
    from investment_panel.database.thesis import save_thesis

    save_thesis(config, "PRES", {"thesis": "Initial monitored setup.", "invalidation": "Below $300."})
    cfg = _config(tmp_path, migrated_postgres_dsn)
    monkeypatch.setattr(
        run_thesis_monitor,
        "generate_openai_thesis_monitor",
        lambda _request: (_ for _ in ()).throw(OpenAIOptionAgentError("missing authentication")),
    )

    result = run_thesis_monitor.run(str(cfg), force=True)
    history = thesis_history(config, "PRES")

    assert result["failed"] == 1
    assert len(history["revisions"]) == 1
    assert history["revisions"][0]["thesis_json"]["core_thesis"] == "Initial monitored setup."


def test_thesis_automation_debounces_material_event_runs(migrated_postgres_dsn: str, tmp_path: Path, monkeypatch) -> None:
    _watch(migrated_postgres_dsn, "DBNC")
    cfg = _config(tmp_path, migrated_postgres_dsn)
    monkeypatch.setattr(run_thesis_monitor, "generate_openai_thesis_monitor", lambda _request: _model_output("DBNC"))

    first = run_thesis_monitor.run(str(cfg), trigger="material_event", force=False)
    second = run_thesis_monitor.run(str(cfg), trigger="material_event", force=False)

    assert first["completed"] == 1
    assert second["skipped"] == 1
    assert second["results"][0]["reason"] == "debounced"


def test_thesis_automation_dry_run_is_non_writing(migrated_postgres_dsn: str, tmp_path: Path, monkeypatch) -> None:
    _watch(migrated_postgres_dsn, "THIN")
    cfg = _config(tmp_path, migrated_postgres_dsn)
    monkeypatch.setattr(run_thesis_monitor, "generate_openai_thesis_monitor", lambda _request: _model_output("THIN"))

    result = run_thesis_monitor.run(str(cfg), force=True, dry_run=True)

    assert result["skipped"] == 1
    assert result["results"][0]["reason"] == "dry_run_valid"
    with psycopg.connect(migrated_postgres_dsn) as connection:
        runs = connection.execute("SELECT count(*) FROM app.thesis_automation_run").fetchone()[0]
        revisions = connection.execute("SELECT count(*) FROM app.thesis").fetchone()[0]
    assert runs == 0
    assert revisions == 0


def _cleanup_symbols(dsn: str) -> None:
    with psycopg.connect(dsn) as connection:
        instrument_ids = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM catalog.instrument WHERE symbol = ANY(%s)",
                [sorted(_TEST_SYMBOLS)],
            ).fetchall()
        ]
        if not instrument_ids:
            return
        connection.execute("DELETE FROM app.thesis_evidence_assessment WHERE instrument_id = ANY(%s)", [instrument_ids])
        connection.execute("DELETE FROM app.thesis_expression WHERE instrument_id = ANY(%s)", [instrument_ids])
        connection.execute("DELETE FROM app.thesis_review_event WHERE instrument_id = ANY(%s)", [instrument_ids])
        connection.execute("DELETE FROM app.thesis WHERE instrument_id = ANY(%s)", [instrument_ids])
        connection.execute("DELETE FROM app.thesis_automation_run WHERE instrument_id = ANY(%s)", [instrument_ids])
        connection.execute("DELETE FROM app.alert WHERE instrument_id = ANY(%s)", [instrument_ids])
        connection.execute("DELETE FROM app.watchlist_item WHERE instrument_id = ANY(%s)", [instrument_ids])
        connection.execute("DELETE FROM catalog.instrument WHERE id = ANY(%s)", [instrument_ids])
        connection.commit()


def _watch(dsn: str, symbol: str) -> None:
    runtime = runtime_for_config({"database": {"url": dsn}})
    with runtime.transaction() as connection:
        instrument_id = reconcile_instrument(connection, symbol, name=symbol, category="watchlist")
        connection.execute(
            "INSERT INTO app.watchlist_item (instrument_id, watch_state) VALUES (%s, 'watched') "
            "ON CONFLICT (instrument_id) DO UPDATE SET watch_state = 'watched'",
            [instrument_id],
        )


def _config(tmp_path: Path, dsn: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
database:
  url: "{dsn}"
agents:
  thesis_monitor:
    enabled: true
    provider: openai
    model: gpt-test
    reasoning_effort: medium
    prompt_version: thesis_v3_test
    concurrency: 2
    evidence_items_per_symbol: 12
    debounce_minutes: 30
    max_material_runs_per_symbol_per_day: 2
""",
        encoding="utf-8",
    )
    return path


def _model_output(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "change_rationale": "Initial automated v3 thesis.",
        "thesis": {
            "core_thesis": f"{symbol} has a monitored business setup with limited current evidence.",
            "why_owned_watched": "Owned or watched for portfolio research coverage.",
            "direction": "long",
            "timeframe": "12 months",
            "horizon_date": "2027-07-25",
            "conviction": "medium",
            "confidence": "low",
            "pillars": [{"id": "p1", "title": "Coverage", "claim": "Evidence coverage is thin.", "evidence_refs": []}],
            "scenarios": {
                "base": {"probability": 0.6, "target": None, "rationale": "Base case remains under review."},
                "bull": {"probability": 0.25, "target": None, "rationale": "Upside requires corroboration."},
                "bear": {"probability": 0.15, "target": None, "rationale": "Downside if thesis evidence weakens."},
            },
            "catalysts": [],
            "invalidation_rules": [{
                "id": "inv1",
                "type": "time",
                "operator": "<=",
                "text": "Review expires at horizon without corroborating evidence.",
                "price": None,
                "metric": None,
                "event": None,
                "date": "2027-07-25",
            }],
            "review_cadence_days": 45,
            "next_review_date": "2026-09-08",
            "lifecycle_status": "active",
            "evidence_coverage_status": "low",
            "automation_policy": "auto",
            "evidence_links": [],
        },
        "evidence_assessments": [],
        "_meta": {"usage": {"input_tokens": 10, "output_tokens": 20}},
    }
