"""Radar alert materialization and acknowledgement helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from investment_panel.core.db import json_dumps, query_rows
from investment_panel.core.options_radar_coerce import _normalize_symbol, _number
from investment_panel.core.options_radar_constants import (
    DATA_CONTRACT_READY,
    DEFAULT_STRATEGY_VERSION,
    EXCEPTIONAL_CONVICTION_BAR,
    RADAR_ALERT_DEDUP_HOURS,
    RADAR_ALERT_TYPES,
    SERVICE_BUG_TIER,
)
from investment_panel.core.options_radar_filters import _symbol_filter
from investment_panel.core.source_ingestion.utils import stable_id


def refresh_radar_alerts(
    con: Any,
    *,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    symbols: list[str] | None = None,
    resolve_all: bool = True,
) -> int:
    """Materialize actionable radar alerts from the current opportunity read model."""

    symbol_filter = _symbol_filter(symbols, table_alias="oro", column="ticker")
    rows = query_rows(
        con,
        f"""
        WITH latest AS (
            SELECT max(snapshot_time) AS snapshot_time
            FROM option_radar_opportunity oro
            WHERE oro.strategy_version = ? {symbol_filter["sql"]}
        )
        SELECT oro.opportunity_id, oro.snapshot_time, oro.ticker, oro.strategy_version, oro.tier,
               oro.primary_event_id, oro.primary_contract_id, oro.primary_state,
               oro.conviction_score, oro.premium_mid, oro.premium_fill_assumption,
               oro.buy_under, oro.required_move_pct, oro.data_contract_status,
               oro.service_repair_summary, oro.quality_status, oro.quality_flags, oro.raw
        FROM option_radar_opportunity oro
        WHERE oro.strategy_version = ?
          AND snapshot_time = (SELECT snapshot_time FROM latest)
          {symbol_filter["sql"]}
        """,
        [strategy_version, *symbol_filter["params"], strategy_version, *symbol_filter["params"]],
    )
    recent = query_rows(
        con,
        f"""
        SELECT alert_type, coalesce(event_id, '') AS event_id, coalesce(contract_id, '') AS contract_id
        FROM radar_alert
        WHERE created_at >= current_timestamp - INTERVAL '{RADAR_ALERT_DEDUP_HOURS} hours'
          AND (acknowledged_at IS NULL OR resolution_reason = 'manual_ack')
        """,
    )
    created_at = datetime.utcnow().isoformat()
    alerts = [alert for opportunity in rows for alert in _radar_alerts_for_opportunity(opportunity, created_at)]
    current_identities = {(alert["alert_type"], alert["event_id"] or "", alert["contract_id"] or "") for alert in alerts}
    resolve_symbols = None if resolve_all else {_normalize_symbol(symbol) for symbol in symbols or [] if symbol}
    _resolve_stale_radar_alerts(con, current_identities, resolved_at=created_at, symbols=resolve_symbols)

    seen = {(str(row.get("alert_type") or ""), str(row.get("event_id") or ""), str(row.get("contract_id") or "")) for row in recent}
    count = 0
    for alert in alerts:
        identity = (alert["alert_type"], alert["event_id"] or "", alert["contract_id"] or "")
        if identity in seen:
            continue
        seen.add(identity)
        con.execute(
            """
            INSERT OR REPLACE INTO radar_alert
            (alert_id, created_at, alert_type, ticker, contract_id, event_id,
             severity, title, detail, acknowledged_at, resolution_reason, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                alert["alert_id"],
                alert["created_at"],
                alert["alert_type"],
                alert["ticker"],
                alert["contract_id"],
                alert["event_id"],
                alert["severity"],
                alert["title"],
                alert["detail"],
                None,
                None,
                json_dumps(alert["raw"]),
            ],
        )
        count += 1
    return count


def _resolve_stale_radar_alerts(
    con: Any,
    current_identities: set[tuple[str, str, str]],
    *,
    resolved_at: str,
    symbols: set[str] | None,
) -> None:
    symbol_filter = ""
    params: list[Any] = []
    if symbols is not None:
        if not symbols:
            return
        placeholders = ", ".join("?" for _ in symbols)
        symbol_filter = f"AND ticker IN ({placeholders})"
        params.extend(sorted(symbols))
    active = query_rows(
        con,
        f"""
        SELECT alert_id, alert_type, coalesce(event_id, '') AS event_id, coalesce(contract_id, '') AS contract_id
        FROM radar_alert
        WHERE acknowledged_at IS NULL
        {symbol_filter}
        """,
        params,
    )
    for alert in active:
        alert_type = str(alert.get("alert_type") or "")
        identity = (alert_type, str(alert.get("event_id") or ""), str(alert.get("contract_id") or ""))
        if alert_type in RADAR_ALERT_TYPES and identity not in current_identities:
            con.execute(
                """
                UPDATE radar_alert
                SET acknowledged_at = TRY_CAST(? AS TIMESTAMP),
                    resolution_reason = 'auto_resolved'
                WHERE alert_id = ?
                """,
                [resolved_at, alert.get("alert_id")],
            )


def acknowledge_radar_alert(con: Any, alert_id: str, *, acknowledged_at: str | None = None) -> bool:
    timestamp = acknowledged_at or datetime.utcnow().isoformat()
    existing = query_rows(con, "SELECT alert_id FROM radar_alert WHERE alert_id = ? LIMIT 1", [alert_id])
    if not existing:
        return False
    con.execute(
        """
        UPDATE radar_alert
        SET acknowledged_at = TRY_CAST(? AS TIMESTAMP),
            resolution_reason = 'manual_ack'
        WHERE alert_id = ?
        """,
        [timestamp, alert_id],
    )
    return True


def _radar_alerts_for_opportunity(row: dict[str, Any], created_at: str) -> list[dict[str, Any]]:
    ticker = _normalize_symbol(row.get("ticker"))
    contract_id = str(row.get("primary_contract_id") or "")
    event_id = str(row.get("primary_event_id") or "")
    tier = str(row.get("tier") or "")
    primary_state = str(row.get("primary_state") or "").upper()
    buy_under = _number(row.get("buy_under"))
    premium = _number(row.get("premium_mid"))
    conviction = _number(row.get("conviction_score"))
    quality = str(row.get("quality_status") or "ok").lower()
    data_status = str(row.get("data_contract_status") or DATA_CONTRACT_READY).lower()
    output: list[dict[str, Any]] = []

    def add(alert_type: str, severity: str, title: str, detail: str) -> None:
        output.append(
            {
                "alert_id": stable_id("radar_alert", alert_type, event_id or contract_id, created_at[:13]),
                "created_at": created_at,
                "alert_type": alert_type,
                "ticker": ticker,
                "contract_id": contract_id,
                "event_id": event_id,
                "severity": severity,
                "title": title,
                "detail": detail,
                "raw": {
                    "tier": tier,
                    "primary_state": primary_state,
                    "conviction_score": conviction,
                    "premium_mid": premium,
                    "buy_under": buy_under,
                    "quality_status": quality,
                    "data_contract_status": data_status,
                },
            }
        )

    if data_status != DATA_CONTRACT_READY or quality == "bad" or tier == SERVICE_BUG_TIER:
        add("data_contract", "critical", f"{ticker} data contract blocked", str(row.get("service_repair_summary") or "Fix radar data contract before trade review."))
    if tier == "Exceptional" and conviction is not None and conviction >= EXCEPTIONAL_CONVICTION_BAR:
        add("exceptional_conviction", "critical", f"{ticker} trade-ready signal", f"Conviction {conviction:.0f}; {contract_id} is the current primary contract.")
    if primary_state in {"FIRE", "SETUP"} and buy_under is not None and premium is not None and premium <= buy_under:
        add("buy_under_hit", "warning", f"{ticker} premium inside cap", f"Premium {premium:.2f} is at or below buy-under {buy_under:.2f}.")
    return output
