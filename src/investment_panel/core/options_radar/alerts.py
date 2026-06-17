"""Radar alerts, acknowledgement, and the trade journal."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from investment_panel.core.db import (json_dumps, query_rows)
from investment_panel.core.source_ingestion.utils import (stable_id)
from investment_panel.core.options_radar.coerce import (_json, _normalize_symbol, _number)
from investment_panel.core.options_radar.constants import (
    DATA_CONTRACT_READY,
    DEFAULT_STRATEGY_VERSION,
    EXCEPTIONAL_CONVICTION_BAR,
    RADAR_ALERT_DEDUP_HOURS,
    RADAR_ALERT_TYPES,
    SERVICE_BUG_TIER,
)

RADAR_ALERT_CONVICTION_BAR = EXCEPTIONAL_CONVICTION_BAR


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


def refresh_radar_alerts(
    con: Any,
    *,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    symbols: list[str] | None = None,
    resolve_all: bool = True,
) -> int:
    """Materialize actionable radar alerts from the current opportunity read model."""

    symbol_filter = ""
    symbol_params: list[Any] = []
    clean_symbols = [_normalize_symbol(symbol) for symbol in symbols or [] if symbol]
    if clean_symbols:
        placeholders = ", ".join("?" for _ in clean_symbols)
        symbol_filter = f"AND oro.ticker IN ({placeholders})"
        symbol_params = clean_symbols

    rows = query_rows(
        con,
        f"""
        WITH latest AS (
            SELECT max(snapshot_time) AS snapshot_time
            FROM option_radar_opportunity oro
            WHERE oro.strategy_version = ? {symbol_filter}
        )
        SELECT oro.opportunity_id, oro.snapshot_time, oro.ticker, oro.strategy_version, oro.tier,
               oro.primary_event_id, oro.primary_contract_id, oro.primary_state,
               oro.conviction_score, oro.premium_mid, oro.premium_fill_assumption,
               oro.buy_under, oro.required_move_pct, oro.data_contract_status,
               oro.service_repair_summary, oro.quality_status, oro.quality_flags, oro.raw
        FROM option_radar_opportunity oro
        WHERE oro.strategy_version = ?
          AND ((SELECT snapshot_time FROM latest) IS NULL OR snapshot_time = (SELECT snapshot_time FROM latest))
          {symbol_filter}
        """,
        [strategy_version, *symbol_params, strategy_version, *symbol_params],
    )
    recent = query_rows(
        con,
        f"""
        SELECT alert_type, coalesce(event_id, '') AS event_id, coalesce(contract_id, '') AS contract_id
        FROM radar_alert
        WHERE created_at >= current_timestamp - INTERVAL '{RADAR_ALERT_DEDUP_HOURS} hours'
          AND strategy_version = ?
          AND (acknowledged_at IS NULL OR resolution_reason = 'manual_ack')
        """,
        [strategy_version],
    )
    created_at = datetime.utcnow().isoformat()
    alerts = [alert for opportunity in rows for alert in _radar_alerts_for_opportunity(opportunity, created_at)]
    current_identities = {(alert["alert_type"], alert["event_id"] or "", alert["contract_id"] or "") for alert in alerts}
    resolve_symbols = None if resolve_all else set(clean_symbols)
    _resolve_stale_radar_alerts(con, current_identities, strategy_version=strategy_version, resolved_at=created_at, symbols=resolve_symbols)

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
            (alert_id, created_at, strategy_version, alert_type, ticker, contract_id, event_id,
             severity, title, detail, acknowledged_at, resolution_reason, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                alert["alert_id"],
                alert["created_at"],
                strategy_version,
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
    strategy_version: str,
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
          AND strategy_version = ?
        {symbol_filter}
        """,
        [strategy_version, *params],
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
    if tier in {"", "Exceptional"} and conviction is not None and conviction >= EXCEPTIONAL_CONVICTION_BAR:
        add("exceptional_conviction", "critical", f"{ticker} trade-ready signal", f"Conviction {conviction:.0f}; {contract_id} is the current primary contract.")
    if primary_state in {"FIRE", "SETUP"} and buy_under is not None and premium is not None and premium <= buy_under:
        add("buy_under_hit", "warning", f"{ticker} premium inside cap", f"Premium {premium:.2f} is at or below buy-under {buy_under:.2f}.")
    return output


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
