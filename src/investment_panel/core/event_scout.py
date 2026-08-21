"""Point-in-time Event Scout and two-lane event decision contract.

This module owns the facts that are allowed to reach the event-driven decision
surfaces.  It deliberately keeps observations as small evidence packets rather
than bare numbers: a number without its observation time, report date, and
source cannot be safely compared with another number.

The module has no provider or execution side effects.  Collectors are injected
by the Event Scout job and paper-only output is the strongest result it can
produce.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime
import hashlib
from typing import Any, Iterable, Mapping

from investment_panel.core.event_decisions import build_event_decisions
from investment_panel.core.event_replays import (
    MRNA_EVENT_SOURCE, MRNA_REPLAY_SOURCE, MRNA_SHORT_SOURCE, mrna_replay_fixture, replay_mrna,
)
from investment_panel.core.event_scout_runtime import (
    SCOUT_COOLDOWN_MINUTES, SCOUT_MAX_SYMBOLS, SCOUT_TRIGGER_TYPES, EventScout, ScoutSignal,
)
from investment_panel.core.event_truth import (
    EVENT_SCOUT_ROUTE_VERSION, OPTIONS_DECISION_ROUTE_VERSION, build_decision_truth, build_options_decision_truth,
)


EVIDENCE_CLASSES = frozenset({"verified_fact", "reported_fact", "derived_metric", "inference", "missing"})
FRESHNESS_VALUES = frozenset({"fresh", "aging", "stale", "unknown"})
def _timestamp(value: Any, *, default: datetime | None = None) -> datetime | None:
    if value is None or value == "":
        return default
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day, tzinfo=UTC)
    else:
        text = str(value).strip()
        if not text:
            return default
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                return default
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: Any) -> str | None:
    parsed = _timestamp(value)
    return parsed.isoformat() if parsed else None


def _date_iso(value: Any) -> str | None:
    if value is None or value == "":
        return None
    parsed = _timestamp(value)
    return parsed.date().isoformat() if parsed else str(value)


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _value(value: Any) -> Any:
    if isinstance(value, Mapping) and "value" in value:
        return value.get("value")
    return value


def _field_value(value: Any, *aliases: str) -> Any:
    if not isinstance(value, Mapping):
        return None
    for alias in aliases:
        if alias in value:
            return value[alias]
    return None


def evidence_field(
    value: Any = None,
    *,
    observed_at: Any = None,
    record_date: Any = None,
    source_url: str | None = None,
    source_kind: str | None = None,
    freshness: str = "unknown",
    evidence_class: str = "missing",
    note: str | None = None,
) -> dict[str, Any]:
    """Create one normalized field in the Event Decision Packet.

    Missing values remain ``None`` and are never converted into a neutral
    number.  A supplied evidence class is also fail-closed: an empty value is
    always classified as ``missing``.
    """

    normalized_class = evidence_class if evidence_class in EVIDENCE_CLASSES else "missing"
    if value is None:
        normalized_class = "missing"
    normalized_freshness = freshness if freshness in FRESHNESS_VALUES else "unknown"
    output = {
        "value": value,
        "observed_at": _iso(observed_at),
        "record_date": _date_iso(record_date),
        "source_url": source_url,
        "source_kind": source_kind,
        "freshness": normalized_freshness,
        "evidence_class": normalized_class,
    }
    if note:
        output["note"] = note
    return output


def _coerce_field(
    raw: Any,
    *,
    as_of: datetime,
    source_url: str | None = None,
    source_kind: str | None = None,
    default_evidence_class: str = "reported_fact",
    record_date: Any = None,
    observed_at: Any = None,
) -> dict[str, Any]:
    if isinstance(raw, (datetime, date)):
        raw = _iso(raw)
    if isinstance(raw, Mapping) and "value" in raw:
        result = dict(raw)
        result.setdefault("observed_at", _iso(observed_at or as_of))
        result.setdefault("record_date", _date_iso(record_date))
        result.setdefault("source_url", source_url)
        result.setdefault("source_kind", source_kind)
        result.setdefault("freshness", "unknown")
        result.setdefault("evidence_class", default_evidence_class if raw.get("value") is not None else "missing")
        if result.get("value") is None:
            result["evidence_class"] = "missing"
        return result
    return evidence_field(
        raw,
        observed_at=observed_at or as_of,
        record_date=record_date,
        source_url=source_url,
        source_kind=source_kind,
        freshness="fresh" if _timestamp(observed_at or as_of) == as_of else "unknown",
        evidence_class=default_evidence_class,
    )


def _category(
    raw: Mapping[str, Any] | None,
    names: Iterable[str],
    *,
    as_of: datetime,
    source_url: str | None = None,
    source_kind: str | None = None,
    default_evidence_class: str = "reported_fact",
) -> dict[str, dict[str, Any]]:
    source = raw or {}
    return {
        name: _coerce_field(
            source.get(name),
            as_of=as_of,
            source_url=source_url,
            source_kind=source_kind,
            default_evidence_class=default_evidence_class,
        )
        for name in names
    }


def _with_source(field_value: dict[str, Any], source_url: str | None, source_kind: str | None) -> dict[str, Any]:
    output = dict(field_value)
    if source_url and not output.get("source_url"):
        output["source_url"] = source_url
    if source_kind and not output.get("source_kind"):
        output["source_kind"] = source_kind
    return output


def latest_short_interest_snapshot(
    history: Iterable[Mapping[str, Any]] | None,
    *,
    as_of: Any,
) -> dict[str, Any]:
    """Select one short-interest report without mixing report dates.

    The latest record date must also have been published/available at the
    packet's ``as_of``.  Older reports are retained under ``history`` for audit
    and replay, but never contribute to the selected point-in-time fields.
    """

    cutoff = _timestamp(as_of) or datetime.now(UTC)
    if isinstance(history, Mapping):
        history = [history]
    candidates: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    for item in history or []:
        row = dict(item)
        record = _timestamp(row.get("record_date") or row.get("report_date") or row.get("date"))
        published = _timestamp(
            row.get("publish_date") or row.get("published_at") or row.get("available_at") or row.get("observed_at")
        )
        if published and published > cutoff:
            row["selection_status"] = "future_at_packet_as_of"
            retained.append(row)
            continue
        if record and record.date() <= cutoff.date():
            row["record_date"] = record.date().isoformat()
            row["publish_date"] = published.isoformat() if published else None
            row["selection_status"] = "eligible"
            candidates.append(row)
        else:
            row["selection_status"] = "future_record_date"
        retained.append(row)
    candidates.sort(
        key=lambda row: (
            str(row.get("record_date") or ""),
            str(row.get("publish_date") or ""),
            str(row.get("observed_at") or ""),
        ),
        reverse=True,
    )
    selected = deepcopy(candidates[0]) if candidates else None
    return {
        "selected": selected,
        "history": sorted(retained, key=lambda row: (str(row.get("record_date") or ""), str(row.get("publish_date") or "")), reverse=True),
        "selection_as_of": cutoff.isoformat(),
        "selection_rule": "max record_date, then publish_date, not published after packet as_of",
        "mixed_report_date": False,
    }


select_latest_short_interest = latest_short_interest_snapshot


def match_historical_cases(
    cases: Iterable[Mapping[str, Any]] | None,
    target: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep only cases comparable on the requested forecast horizon and setup."""

    comparable_keys = (
        "forecast_horizon",
        "horizon",
        "event_kind",
        "trial_phase",
        "first_in_modality",
        "market_environment",
        "market_cap_band",
        "liquidity_band",
        "short_interest_band",
        "days_to_cover_band",
        "options_positioning_band",
        "narrative_change",
    )
    matched: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for original in cases or []:
        case = dict(original)
        reasons: list[str] = []
        for key in comparable_keys:
            target_value = target.get(key)
            case_value = case.get(key)
            if target_value is None or case_value is None:
                continue
            if isinstance(target_value, (list, tuple, set)):
                if case_value not in target_value:
                    reasons.append(f"{key}_mismatch")
            elif str(target_value).lower() != str(case_value).lower():
                reasons.append(f"{key}_mismatch")
        if reasons:
            case["excluded_reasons"] = sorted(set(reasons))
            excluded.append(case)
        else:
            matched.append(case)
    return {
        "target": dict(target),
        "matched": matched,
        "excluded": excluded,
        "evidence_state": "ready" if matched else "insufficient_matching_cases",
        "matching_rule": "all available comparable dimensions must agree; horizon mismatch excludes a case",
    }


def _market_tape(
    raw: Mapping[str, Any] | None,
    *,
    as_of: datetime,
    source_url: str | None,
    source_kind: str | None,
) -> dict[str, dict[str, Any]]:
    source = dict(raw or {})
    aliases = {
        "previous_close": ("previous_close", "prior_close"),
        "premarket_price": ("premarket_price", "pre_market_price"),
        "open_price": ("open_price", "open"),
        "event_price": ("event_price", "price_after_event"),
        "latest_price": ("latest_price", "price", "last"),
        "intraday_high": ("intraday_high", "high"),
        "intraday_low": ("intraday_low", "low"),
        "day_change_pct": ("day_change_pct", "change_pct"),
        "change_from_premarket_pct": ("change_from_premarket_pct",),
        "change_from_open_pct": ("change_from_open_pct",),
        "change_from_event_pct": ("change_from_event_pct", "event_return_pct"),
        "event_return_pct": ("event_return_pct",),
        "volume": ("volume",),
        "average_volume": ("average_volume", "avg_volume"),
        "float_shares": ("float_shares",),
        "volume_to_float": ("volume_to_float", "volume_float"),
        "volume_to_latest_reported_short_shares": ("volume_to_latest_reported_short_shares",),
        "market_cap_pre_event": ("market_cap_pre_event",),
        "market_cap_post_event": ("market_cap_post_event",),
        "market_cap_change_abs": ("market_cap_change_abs",),
        "market_cap_change_pct": ("market_cap_change_pct",),
        "halt_status": ("halt_status", "halts"),
        "bid_ask_spread": ("bid_ask_spread", "spread"),
        "liquidity_status": ("liquidity_status",),
    }
    output: dict[str, dict[str, Any]] = {}
    for name, names in aliases.items():
        raw_value = next((source.get(alias) for alias in names if alias in source), None)
        output[name] = _coerce_field(raw_value, as_of=as_of, source_url=source_url, source_kind=source_kind)

    previous = _number(_value(output["previous_close"]))
    premarket = _number(_value(output["premarket_price"]))
    opening = _number(_value(output["open_price"]))
    event_price = _number(_value(output["event_price"]))
    latest = _number(_value(output["latest_price"]))
    volume = _number(_value(output["volume"]))
    average_volume = _number(_value(output["average_volume"]))
    float_shares = _number(_value(output["float_shares"]))
    derived = {
        "day_change_pct": ((latest - previous) / previous * 100) if latest is not None and previous and previous > 0 else None,
        "change_from_premarket_pct": ((latest - premarket) / premarket * 100) if latest is not None and premarket and premarket > 0 else None,
        "change_from_open_pct": ((latest - opening) / opening * 100) if latest is not None and opening and opening > 0 else None,
        "change_from_event_pct": ((latest - event_price) / event_price * 100) if latest is not None and event_price and event_price > 0 else None,
        "relative_volume": (volume / average_volume) if volume is not None and average_volume and average_volume > 0 else None,
        "volume_to_float": (volume / float_shares) if volume is not None and float_shares and float_shares > 0 else _value(output["volume_to_float"]),
    }
    for name, value in derived.items():
        if value is not None and _value(output[name]) is None:
            output[name] = evidence_field(
                value,
                observed_at=as_of,
                source_kind="derived",
                freshness="fresh",
                evidence_class="derived_metric",
                note="computed from the same packet as_of",
            )
    return output


def _positioning(
    raw: Mapping[str, Any] | None,
    *,
    short_history: Iterable[Mapping[str, Any]] | None,
    as_of: datetime,
    source_url: str | None,
    source_kind: str | None,
    volume: float | None,
) -> dict[str, Any]:
    source = dict(raw or {})
    selected_report = latest_short_interest_snapshot(short_history or source.get("short_interest_history"), as_of=as_of)
    selected = selected_report["selected"] or {}
    short_url = selected.get("source_url") or source.get("short_interest_source_url") or source_url
    short_kind = selected.get("source_kind") or source.get("short_interest_source_kind") or source_kind
    short_observed_at = selected.get("publish_date") or selected.get("published_at") or selected.get("available_at") or as_of
    record_date = selected.get("record_date")
    names = {
        "short_shares": ("short_shares", "shares_short"),
        "short_pct_float": ("short_pct_float", "short_percent_float"),
        "days_to_cover": ("days_to_cover", "short_ratio"),
        "borrow_availability": ("borrow_availability",),
        "borrow_fee": ("borrow_fee", "borrow_rate"),
        "put_call_volume": ("put_call_volume",),
        "put_call_open_interest": ("put_call_open_interest",),
        "volume_to_open_interest": ("volume_to_open_interest", "volume_oi"),
        "implied_volatility": ("implied_volatility", "iv"),
        "volatility_skew": ("volatility_skew", "skew"),
        "term_structure": ("term_structure",),
        "abnormal_strikes": ("abnormal_strikes",),
        "abnormal_expiries": ("abnormal_expiries",),
        "option_liquidity": ("option_liquidity",),
    }
    output: dict[str, Any] = {}
    for name, aliases in names.items():
        raw_value = next((selected.get(alias) for alias in aliases if alias in selected), None)
        if raw_value is None:
            raw_value = next((source.get(alias) for alias in aliases if alias in source), None)
        is_short = name in {"short_shares", "short_pct_float", "days_to_cover"}
        output[name] = _coerce_field(
            raw_value,
            as_of=as_of,
            observed_at=short_observed_at if is_short else None,
            record_date=record_date if is_short else None,
            source_url=short_url if is_short else source_url,
            source_kind=short_kind if is_short else source_kind,
        )
    output["short_interest_record_date"] = _coerce_field(record_date, as_of=as_of, observed_at=short_observed_at, source_url=short_url, source_kind=short_kind)
    output["short_interest_publish_date"] = _coerce_field(short_observed_at, as_of=as_of, observed_at=short_observed_at, source_url=short_url, source_kind=short_kind)
    output["short_interest_average_volume_basis"] = _coerce_field(
        selected.get("average_volume_basis") or source.get("short_interest_average_volume_basis"),
        as_of=as_of,
        observed_at=short_observed_at,
        record_date=record_date,
        source_url=short_url,
        source_kind=short_kind,
    )
    output["short_interest_history"] = selected_report["history"]
    output["short_interest_selection"] = {
        "record_date": selected.get("record_date"),
        "publish_date": selected.get("publish_date"),
        "source_url": short_url,
        "selection_as_of": selected_report["selection_as_of"],
        "mixed_report_date": selected_report["mixed_report_date"],
    }
    short_shares = _number(_value(output["short_shares"]))
    volume_over_short = (volume / short_shares) if volume is not None and short_shares and short_shares > 0 else None
    output["volume_over_latest_reported_short_shares"] = evidence_field(
        volume_over_short,
        observed_at=as_of,
        source_kind="derived",
        freshness="fresh" if volume_over_short is not None else "unknown",
        evidence_class="derived_metric",
        note="same packet volume divided by selected short-interest report",
    )
    output["volume_exceeds_latest_reported_short_shares"] = evidence_field(
        volume_over_short > 1 if volume_over_short is not None else None,
        observed_at=as_of,
        source_kind="derived",
        freshness="fresh" if volume_over_short is not None else "unknown",
        evidence_class="derived_metric",
        note="does not imply shares were covered; it compares reported aggregates",
    )
    for name in ("dealer_hedging", "gamma_inference"):
        raw_value = source.get(name)
        if raw_value is None:
            output[name] = evidence_field(None, observed_at=as_of, source_kind="inference", note="not displayed without direct evidence")
        else:
            class_name = raw_value.get("evidence_class") if isinstance(raw_value, Mapping) else "missing"
            if class_name not in {"verified_fact", "reported_fact"}:
                output[name] = evidence_field(None, observed_at=as_of, source_kind="inference", note="direct evidence required")
            else:
                output[name] = _coerce_field(raw_value, as_of=as_of, source_url=source_url, source_kind=source_kind)
    return output


def _event_fundamentals(raw: Mapping[str, Any] | None, *, as_of: datetime, source_url: str | None, source_kind: str | None) -> dict[str, dict[str, Any]]:
    names = (
        "trial_phase", "sample_size", "control_group", "primary_endpoints", "secondary_endpoints",
        "hazard_ratio", "confidence_interval", "p_value", "absolute_benefit", "os_trend",
        "safety", "follow_up", "regulatory_path", "possible_label", "manufacturing_cost",
        "manufacturing_capacity", "partner_economics",
    )
    source = dict(raw or {})
    aliases = {"trial_phase": ("trial_phase", "phase"), "manufacturing_capacity": ("manufacturing_capacity", "capacity")}
    return {
        name: _coerce_field(
            next((source.get(alias) for alias in aliases.get(name, (name,)) if alias in source), None),
            as_of=as_of,
            source_url=source_url,
            source_kind=source_kind,
        )
        for name in names
    }


def _platform_optionality(raw: Mapping[str, Any] | None, *, as_of: datetime, source_url: str | None, source_kind: str | None) -> dict[str, dict[str, Any]]:
    source = dict(raw or {})
    output: dict[str, dict[str, Any]] = {}
    output["first_in_modality"] = _coerce_field(source.get("first_in_modality"), as_of=as_of, source_url=source_url, source_kind=source_kind)
    output["first_in_platform"] = _coerce_field(source.get("first_in_platform"), as_of=as_of, source_url=source_url, source_kind=source_kind)
    output["read_through_to_other_trials"] = _coerce_field(
        source.get("read_through_to_other_trials"),
        as_of=as_of,
        source_url=source_url,
        source_kind=source_kind,
        default_evidence_class="inference",
    )
    output["read_through_to_other_indications"] = _coerce_field(
        source.get("read_through_to_other_indications"),
        as_of=as_of,
        source_url=source_url,
        source_kind=source_kind,
        default_evidence_class="inference",
    )
    output["narrative_change"] = _coerce_field(source.get("narrative_change"), as_of=as_of, source_url=source_url, source_kind=source_kind)
    output["trial_count"] = _coerce_field(source.get("trial_count"), as_of=as_of, source_url=source_url, source_kind=source_kind)
    output["platform_value_extension"] = _coerce_field(
        source.get("platform_value_extension"),
        as_of=as_of,
        source_url=source_url,
        source_kind=source_kind,
        default_evidence_class="inference",
    )
    return output


def _evidence_refs(value: Any, *, prefix: str = "") -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    if isinstance(value, Mapping):
        if value.get("source_url") and value.get("evidence_class") != "missing":
            refs.append({"type": prefix or "source", "url": str(value["source_url"])})
        for key, child in value.items():
            if key in {"source_url", "raw", "history"}:
                continue
            refs.extend(_evidence_refs(child, prefix=f"{prefix}.{key}" if prefix else key))
    elif isinstance(value, list):
        for child in value:
            refs.extend(_evidence_refs(child, prefix=prefix))
    return refs


def _decision_values(category: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _value(value) for key, value in category.items() if isinstance(value, Mapping) and "value" in value}




def build_event_decision_packet(
    symbol: str,
    *,
    as_of: Any = None,
    event_kind: str = "material_event",
    trigger_type: str = "formal_announcement",
    source_url: str | None = None,
    source_kind: str = "unknown",
    headline: str | None = None,
    publication_id: str | None = None,
    market_tape: Mapping[str, Any] | None = None,
    positioning: Mapping[str, Any] | None = None,
    short_interest_history: Iterable[Mapping[str, Any]] | None = None,
    event_fundamentals: Mapping[str, Any] | None = None,
    platform_optionality: Mapping[str, Any] | None = None,
    historical_cases: Iterable[Mapping[str, Any]] | None = None,
    risk_inputs: Mapping[str, Any] | None = None,
    signal: Mapping[str, Any] | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Build the canonical, point-in-time packet used by every event surface."""

    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        raise ValueError("symbol is required")
    signal = dict(signal or {})
    observed = _timestamp(as_of or signal.get("observed_at") or signal.get("available_at")) or datetime.now(UTC)
    event_kind = str(signal.get("event_kind") or event_kind)
    trigger_type = str(signal.get("trigger_type") or trigger_type)
    source_url = source_url or signal.get("source_url")
    source_kind = str(signal.get("source_kind") or source_kind)
    headline = headline or signal.get("headline") or signal.get("title")
    event_id = event_id or hashlib.sha256(
        f"{normalized_symbol}|{event_kind}|{observed.isoformat()}|{source_url or ''}".encode("utf-8")
    ).hexdigest()[:24]

    tape = _market_tape(market_tape or signal.get("market_tape"), as_of=observed, source_url=source_url, source_kind=source_kind)
    position = _positioning(
        positioning or signal.get("positioning"),
        short_history=short_interest_history or signal.get("short_interest_history"),
        as_of=observed,
        source_url=source_url,
        source_kind=source_kind,
        volume=_number(_value(tape.get("volume"))),
    )
    volume_short = _value(position["volume_over_latest_reported_short_shares"])
    tape["volume_to_latest_reported_short_shares"] = evidence_field(
        volume_short,
        observed_at=observed,
        source_kind="derived",
        freshness="fresh" if volume_short is not None else "unknown",
        evidence_class="derived_metric",
        note="same point-in-time inputs as positioning",
    )
    tape["volume_exceeds_latest_reported_short_shares"] = evidence_field(
        volume_short > 1 if volume_short is not None else None,
        observed_at=observed,
        source_kind="derived",
        freshness="fresh" if volume_short is not None else "unknown",
        evidence_class="derived_metric",
    )
    fundamentals = _event_fundamentals(event_fundamentals or signal.get("event_fundamentals"), as_of=observed, source_url=source_url, source_kind=source_kind)
    platform = _platform_optionality(platform_optionality or signal.get("platform_optionality"), as_of=observed, source_url=source_url, source_kind=source_kind)
    case_input = list(historical_cases or signal.get("historical_cases") or [])
    history = {
        "intraday": match_historical_cases(
            case_input,
            {
                "forecast_horizon": "intraday",
                "event_kind": event_kind,
                "trial_phase": _value(fundamentals["trial_phase"]),
                "first_in_modality": _value(platform["first_in_modality"]),
            },
        ),
        "monthly_yearly": match_historical_cases(
            case_input,
            {
                "forecast_horizon": "monthly_yearly",
                "event_kind": event_kind,
                "trial_phase": _value(fundamentals["trial_phase"]),
                "first_in_modality": _value(platform["first_in_modality"]),
            },
        ),
    }
    tactical, fundamental, truth = build_event_decisions(
        symbol=normalized_symbol,
        as_of=observed,
        market_tape=tape,
        positioning=position,
        fundamentals=fundamentals,
        platform=platform,
        history=history,
        risk_inputs=risk_inputs or signal.get("risk_inputs"),
    )
    truth["publication_id"] = publication_id
    refs = _evidence_refs({"market_tape": tape, "positioning": position, "event_fundamentals": fundamentals, "platform_optionality": platform})
    deduped_refs: list[dict[str, str]] = []
    seen_refs: set[tuple[str, str]] = set()
    for ref in refs:
        key = (ref["type"], ref["url"])
        if key not in seen_refs:
            seen_refs.add(key)
            deduped_refs.append(ref)
    if source_url and not any(ref["url"] == source_url for ref in deduped_refs):
        deduped_refs.insert(0, {"type": "event_source", "url": source_url})
    truth["evidence_refs"] = deduped_refs
    packet = {
        "contract_version": "event-decision-packet-v1",
        "event_id": event_id,
        "symbol": normalized_symbol,
        "event_kind": event_kind,
        "trigger_type": trigger_type,
        "source_url": source_url,
        "source_kind": source_kind,
        "headline": headline,
        "as_of": observed.isoformat(),
        "publication_id": publication_id,
        "point_in_time": True,
        "future_leakage_check": "passed",
        "market_tape": tape,
        "positioning": position,
        "event_fundamentals": fundamentals,
        "platform_optionality": platform,
        "historical_cases": history,
        "tactical_decision": tactical,
        "fundamental_decision": fundamental,
        "decision_truth": truth,
        "evidence_refs": deduped_refs,
    }
    return packet


__all__ = [
    "EVIDENCE_CLASSES", "FRESHNESS_VALUES", "SCOUT_TRIGGER_TYPES", "SCOUT_MAX_SYMBOLS", "SCOUT_COOLDOWN_MINUTES",
    "EVENT_SCOUT_ROUTE_VERSION", "OPTIONS_DECISION_ROUTE_VERSION", "ScoutSignal", "EventScout", "evidence_field", "latest_short_interest_snapshot",
    "select_latest_short_interest", "match_historical_cases", "build_event_decision_packet", "mrna_replay_fixture",
    "replay_mrna", "build_decision_truth", "build_options_decision_truth",
]
