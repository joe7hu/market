"""Radar alerts, acknowledgement, and the trade journal."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from investment_panel.core.db import (json_dumps, query_rows)
from investment_panel.core.source_ingestion.utils import (stable_id)
from investment_panel.core.options_radar.coerce import (_json, _normalize_symbol, _number)
from investment_panel.core.options_radar.constants import (DEFAULT_STRATEGY_VERSION)

RADAR_ALERT_CONVICTION_BAR = 78.0


def build_radar_alerts(
    opportunities: list[dict[str, Any]],
    flow_by_contract: dict[str, float | None],
    existing_keys: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Derive new radar alerts from the current opportunity set, deduping against
    already-open (unacknowledged) ``(alert_type, contract_id)`` keys.

    Fires on: premium dropping inside the EV buy-under, an exceptional-conviction
    FIRE, and a >=2 sigma OI-flow spike on the chosen contract."""

    alerts: list[dict[str, Any]] = []
    for opp in opportunities:
        ticker = _normalize_symbol(opp.get("ticker"))
        contract = str(opp.get("primary_contract_id") or "")
        event_id = opp.get("primary_event_id")
        state = str(opp.get("primary_state") or "").upper()
        premium = _number(opp.get("premium_mid"))
        buy_under = _number(opp.get("buy_under"))
        conviction = _number(opp.get("conviction_score")) or 0.0

        candidates: list[tuple[str, str, str]] = []
        if buy_under is not None and premium is not None and premium < buy_under and state in {"FIRE", "SETUP"}:
            candidates.append(("premium_below_buy_under", "high", f"{ticker} {contract}: premium ${premium:.2f} is inside the EV buy-under ${buy_under:.2f}"))
        if conviction >= RADAR_ALERT_CONVICTION_BAR and state == "FIRE":
            candidates.append(("exceptional_conviction", "high", f"{ticker} {contract}: FIRE at conviction {conviction:.0f}"))
        zscore = _number(flow_by_contract.get(contract))
        if zscore is not None and zscore >= 2.0:
            candidates.append(("flow_oi_spike", "medium", f"{ticker} {contract}: open-interest expansion {zscore:.1f} sigma"))

        for alert_type, severity, message in candidates:
            if (alert_type, contract) in existing_keys:
                continue
            existing_keys.add((alert_type, contract))
            alerts.append(
                {
                    "alert_type": alert_type,
                    "ticker": ticker,
                    "contract_id": contract,
                    "event_id": event_id,
                    "severity": severity,
                    "message": message,
                }
            )
    return alerts


def refresh_radar_alerts(con: Any, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> int:
    """Evaluate alert conditions against the freshly-built opportunity set and persist
    new (deduped) alerts. Runs in the fast pass — not gated behind the learning loop."""

    existing = query_rows(
        con,
        "SELECT alert_type, contract_id FROM radar_alert WHERE strategy_version = ? AND acknowledged_at IS NULL",
        [strategy_version],
    )
    existing_keys = {(str(r.get("alert_type")), str(r.get("contract_id"))) for r in existing}
    opportunities = query_rows(
        con,
        "SELECT ticker, primary_contract_id, primary_event_id, primary_state, premium_mid, buy_under, conviction_score "
        "FROM option_radar_opportunity WHERE strategy_version = ?",
        [strategy_version],
    )
    flow_rows = query_rows(
        con,
        """
        SELECT contract_id, oi_zscore_20d
        FROM option_flow_features
        QUALIFY row_number() OVER (PARTITION BY contract_id ORDER BY snapshot_time DESC) = 1
        """,
    )
    flow_by_contract = {str(r.get("contract_id")): _number(r.get("oi_zscore_20d")) for r in flow_rows}

    new_alerts = build_radar_alerts(opportunities, flow_by_contract, existing_keys)
    created_at = datetime.now(timezone.utc).isoformat()
    for alert in new_alerts:
        alert_id = stable_id("radar_alert", strategy_version, alert["alert_type"], alert["contract_id"], created_at)
        con.execute(
            """
            INSERT OR REPLACE INTO radar_alert
            (alert_id, created_at, strategy_version, alert_type, ticker, contract_id,
             event_id, severity, message, acknowledged_at, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            [
                alert_id,
                created_at,
                strategy_version,
                alert["alert_type"],
                alert["ticker"],
                alert["contract_id"],
                alert["event_id"],
                alert["severity"],
                alert["message"],
                json_dumps({}),
            ],
        )
    return len(new_alerts)


def record_trade_journal_entry(
    con: Any,
    *,
    ticker: str,
    contract_id: str,
    event_id: str | None = None,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    opportunity: dict[str, Any] | None = None,
    notes: str = "",
) -> str:
    """Capture the full opportunity JSON at click into the trade journal — the
    real-money set the calibration dashboard grades predicted-vs-realized against.
    Returns the journal_id."""

    opportunity = opportunity or {}
    ev = _json(opportunity.get("raw")).get("primary_detail", {}) if isinstance(opportunity.get("raw"), (str, dict)) else {}
    created_at = datetime.now(timezone.utc).isoformat()
    journal_id = stable_id("trade_journal", strategy_version, contract_id, created_at)
    con.execute(
        """
        INSERT OR REPLACE INTO trade_journal
        (journal_id, created_at, strategy_version, ticker, contract_id, event_id,
         entry_premium, predicted_ev_multiple, predicted_p2x, conviction_score,
         opportunity_snapshot, realized_return, realized_status, closed_at, notes, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'open', NULL, ?, ?)
        """,
        [
            journal_id,
            created_at,
            strategy_version,
            _normalize_symbol(ticker),
            str(contract_id),
            event_id,
            _number(opportunity.get("premium_mid")),
            _number((ev or {}).get("ev_multiple")),
            _number((ev or {}).get("calibrated_p2x") or (ev or {}).get("p_2x")),
            _number(opportunity.get("conviction_score")),
            json_dumps(opportunity),
            notes,
            json_dumps({}),
        ],
    )
    return journal_id


def acknowledge_radar_alert(con: Any, alert_id: str) -> int:
    """Mark an alert acknowledged. Returns rows updated (0 if unknown id)."""

    before = query_rows(con, "SELECT alert_id FROM radar_alert WHERE alert_id = ? AND acknowledged_at IS NULL", [alert_id])
    if not before:
        return 0
    con.execute(
        "UPDATE radar_alert SET acknowledged_at = ? WHERE alert_id = ?",
        [datetime.now(timezone.utc).isoformat(), alert_id],
    )
    return 1
