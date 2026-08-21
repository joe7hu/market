"""Bounded Event Scout signal gate and collector orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Iterable, Mapping


SCOUT_TRIGGER_TYPES = frozenset({
    "formal_announcement", "regulatory_announcement", "credible_news",
    "abnormal_gap", "abnormal_price", "abnormal_volume", "abnormal_options_flow",
    "calendar_event",
})
SCOUT_MAX_SYMBOLS = 5
SCOUT_COOLDOWN_MINUTES = 30


def _timestamp(value: Any, *, default: datetime | None = None) -> datetime | None:
    from investment_panel.core.event_scout import _timestamp as parse_timestamp

    return parse_timestamp(value, default=default)


@dataclass
class ScoutSignal:
    symbol: str
    trigger_type: str
    observed_at: datetime
    event_kind: str = "material_event"
    source_url: str | None = None
    source_kind: str = "unknown"
    headline: str | None = None
    payload: dict[str, Any] = dataclass_field(default_factory=dict)


class EventScout:
    """Bounded signal gate for the five-minute Event Scout workflow."""

    def __init__(
        self,
        *,
        cooldown_minutes: int = SCOUT_COOLDOWN_MINUTES,
        max_symbols: int = SCOUT_MAX_SYMBOLS,
        last_seen: Mapping[str, Any] | None = None,
    ) -> None:
        self.cooldown = timedelta(minutes=max(1, cooldown_minutes))
        self.max_symbols = max(1, max_symbols)
        self._last_seen = {
            str(symbol).strip().upper(): parsed
            for symbol, value in (last_seen or {}).items()
            if (parsed := _timestamp(value)) is not None
        }

    def accept_signals(self, signals: Iterable[Mapping[str, Any] | ScoutSignal], *, now: Any = None) -> list[dict[str, Any]]:
        reference = _timestamp(now) or datetime.now(UTC)
        accepted: list[dict[str, Any]] = []
        symbols: set[str] = set()
        for raw in signals:
            signal = raw if isinstance(raw, ScoutSignal) else ScoutSignal(
                symbol=str(raw.get("symbol") or "").strip().upper(),
                trigger_type=str(raw.get("trigger_type") or raw.get("signal_type") or ""),
                observed_at=_timestamp(raw.get("observed_at") or raw.get("as_of"), default=reference) or reference,
                event_kind=str(raw.get("event_kind") or "material_event"),
                source_url=raw.get("source_url"),
                source_kind=str(raw.get("source_kind") or "unknown"),
                headline=raw.get("headline") or raw.get("title"),
                payload=dict(raw.get("payload") or raw.get("data") or {}),
            )
            symbol = signal.symbol.strip().upper()
            if not symbol or signal.trigger_type not in SCOUT_TRIGGER_TYPES or symbol in symbols:
                continue
            prior = self._last_seen.get(symbol)
            if prior and reference < prior + self.cooldown:
                continue
            if len(accepted) >= self.max_symbols:
                break
            self._last_seen[symbol] = reference
            symbols.add(symbol)
            accepted.append({
                "symbol": symbol,
                "trigger_type": signal.trigger_type,
                "event_kind": signal.event_kind,
                "observed_at": signal.observed_at.isoformat(),
                "source_url": signal.source_url,
                "source_kind": signal.source_kind,
                "headline": signal.headline,
                "cooldown_until": (reference + self.cooldown).isoformat(),
                "status": "accepted",
            })
        return accepted

    def process_signal(
        self,
        signal: Mapping[str, Any],
        *,
        collectors: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any] | None]] | None = None,
        now: Any = None,
    ) -> dict[str, Any]:
        from investment_panel.core.event_scout import build_event_decision_packet

        accepted = self.accept_signals([signal], now=now)
        if not accepted:
            return {"status": "cooldown_or_invalid", "accepted": False, "symbol": str(signal.get("symbol") or "").upper()}
        event = accepted[0]
        payload = dict(signal.get("payload") or signal.get("data") or {})
        collector_status: dict[str, Any] = {}
        collector_data = dict(payload)
        for name in ("market_tape", "short_interest", "options_chain", "event_fundamentals", "platform_optionality"):
            callback = (collectors or {}).get(name)
            if callback is None:
                collector_status[name] = {"status": "requested", "result": "missing"}
                continue
            result = callback(signal)
            collector_status[name] = {"status": "collected" if result is not None else "missing"}
            if result:
                normalized_result = dict(result)
                if name == "short_interest":
                    collector_data["short_interest_history"] = normalized_result
                elif name == "options_chain":
                    collector_data["options_chain"] = normalized_result
                    option_metrics = normalized_result.get("positioning")
                    if not isinstance(option_metrics, Mapping):
                        option_metrics = normalized_result
                    positioning = dict(collector_data.get("positioning") or {})
                    for key in (
                        "put_call_volume", "put_call_open_interest", "volume_to_open_interest",
                        "implied_volatility", "volatility_skew", "term_structure",
                        "abnormal_strikes", "abnormal_expiries", "option_liquidity",
                    ):
                        if key in option_metrics:
                            positioning[key] = option_metrics[key]
                    collector_data["positioning"] = positioning
                else:
                    collector_data[name] = normalized_result
        packet = build_event_decision_packet(
            event["symbol"],
            as_of=event["observed_at"],
            event_kind=event["event_kind"],
            trigger_type=event["trigger_type"],
            source_url=event.get("source_url"),
            source_kind=event.get("source_kind") or "unknown",
            headline=event.get("headline"),
            market_tape=collector_data.get("market_tape"),
            positioning=collector_data.get("positioning"),
            short_interest_history=collector_data.get("short_interest_history"),
            event_fundamentals=collector_data.get("event_fundamentals"),
            platform_optionality=collector_data.get("platform_optionality"),
            historical_cases=collector_data.get("historical_cases"),
            risk_inputs=collector_data.get("risk_inputs"),
        )
        event["collection_status"] = collector_status
        event["options_chain_requested"] = True
        return {"status": "accepted", "accepted": True, "scout_event": event, "packet": packet}


__all__ = [
    "SCOUT_TRIGGER_TYPES", "SCOUT_MAX_SYMBOLS", "SCOUT_COOLDOWN_MINUTES",
    "ScoutSignal", "EventScout",
]
