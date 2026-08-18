"""PostgreSQL-owned 13F investor portfolio read model.

This is a disclosure view, not a performance ledger.  13F reports are delayed
and only describe long U.S. reportable positions at quarter end.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any


METHODOLOGY_CAVEAT = (
    "13F filings are delayed quarterly long-position disclosures. They exclude trade timing, "
    "dividends, shorts, undisclosed assets, and corporate-action adjustments."
)


def superinvestor_portfolios(
    connection: Any,
    *,
    investor_key: str | None = None,
    include_holdings: bool = False,
) -> list[dict[str, Any]]:
    query = """
        SELECT trader_name, filer_name, event_date, filed_date, source_id AS source,
               source_url, source_key, payload_id, details
        FROM raw.disclosure
        WHERE source_type = '13f'
    """
    parameters: list[Any] = []
    if investor_key and investor_key.startswith("cik:"):
        query += " AND details->>'cik' = %s"
        parameters.append(investor_key.removeprefix("cik:"))
    query += " ORDER BY trader_name, event_date, filed_date, source_key"
    result = (
        connection.execute(query, parameters)
        if parameters
        else connection.execute(query)
    )
    portfolios = build_superinvestor_portfolios(
        [dict(row) for row in result.fetchall()]
    )
    if investor_key:
        portfolios = [row for row in portfolios if row["investor_key"] == investor_key]
    return portfolios if include_holdings else [_summary(row) for row in portfolios]


def build_superinvestor_portfolios(
    disclosures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compose portfolio rows from normalized disclosure facts (also fixture-friendly)."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for disclosure in disclosures:
        if not isinstance(disclosure.get("details"), dict):
            continue
        investor = str(
            disclosure.get("trader_name")
            or disclosure.get("filer_name")
            or disclosure["details"].get("cik")
            or "Unknown filer"
        )
        grouped[investor].append(disclosure)

    portfolios: list[dict[str, Any]] = []
    for investor, filings in grouped.items():
        ordered = _canonical_filings(filings)
        snapshots = [_snapshot(filing) for filing in ordered]
        if not snapshots:
            continue
        latest = snapshots[-1]
        prior = snapshots[-2] if len(snapshots) > 1 else None
        holdings_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for snapshot in snapshots:
            for holding in snapshot["holdings"]:
                point = {
                    key: holding.get(key)
                    for key in (
                        "symbol",
                        "issuer",
                        "cusip",
                        "put_call",
                        "title",
                        "shares",
                        "reported_value_usd",
                        "implied_price",
                    )
                }
                point.update(
                    {
                        "event_date": snapshot["event_date"],
                        "filed_date": snapshot["filed_date"],
                        "source": snapshot["source"],
                        "source_url": snapshot["source_url"],
                    }
                )
                holdings_by_key[_holding_key(holding)].append(point)
        current = []
        for holding in latest["holdings"]:
            entry = dict(holding)
            entry["history"] = _retained_effect_history(
                holdings_by_key[_holding_key(holding)]
            )
            entry["estimated_retained_share_price_effect"] = _retained_effect(
                entry["history"]
            )
            current.append(entry)
        portfolios.append(
            {
                "investor_key": f"cik:{latest['cik']}"
                if latest.get("cik")
                else f"filer:{investor.lower()}",
                "investor": investor,
                "filer_name": latest["filer_name"],
                "event_date": latest["event_date"],
                "filed_date": latest["filed_date"],
                "source": latest["source"],
                "source_url": latest["source_url"],
                "provenance": {
                    "source_key": latest["source_key"],
                    "payload_id": latest.get("payload_id"),
                    "cik": latest.get("cik"),
                    "form": latest.get("form"),
                },
                "reported_portfolio_value_usd": latest["reported_value_usd"],
                "holdings_count": len(current),
                "filing_history": [
                    {
                        key: snapshot[key]
                        for key in (
                            "event_date",
                            "filed_date",
                            "reported_value_usd",
                            "holdings_count",
                            "source",
                            "source_url",
                            "source_key",
                        )
                    }
                    for snapshot in snapshots
                ],
                "holdings": sorted(
                    current,
                    key=lambda item: item["reported_value_usd"] or 0,
                    reverse=True,
                ),
                "latest_allocation_changes": _changes(prior, latest),
                "methodology_caveat": METHODOLOGY_CAVEAT,
            }
        )
    return sorted(portfolios, key=lambda item: item["investor"].lower())


def _snapshot(filing: dict[str, Any]) -> dict[str, Any]:
    details = filing["details"]
    raw_holdings = (
        details.get("holdings") if isinstance(details.get("holdings"), list) else []
    )
    event_date = _text(filing.get("event_date"))
    value_multiplier = _legacy_value_multiplier(details, event_date, raw_holdings)
    holdings = _aggregate_holdings(
        [
            _holding(row, value_multiplier)
            for row in raw_holdings
            if isinstance(row, dict)
        ]
    )
    value = _number(details.get("holdings_value_usd"))
    if value is not None:
        value *= value_multiplier
    if value is None:
        legacy_value = _number(details.get("holdings_value_thousands"))
        value = legacy_value * value_multiplier if legacy_value is not None else None
    if value is None:
        value = sum(item["reported_value_usd"] or 0 for item in holdings)
    return {
        "filer_name": filing.get("filer_name"),
        "event_date": event_date,
        "filed_date": _text(filing.get("filed_date")),
        "source": filing.get("source"),
        "source_url": filing.get("source_url"),
        "source_key": filing.get("source_key"),
        "payload_id": filing.get("payload_id"),
        "cik": details.get("cik"),
        "form": details.get("form") or filing.get("action"),
        "reported_value_usd": value,
        "holdings_count": len(holdings),
        "holdings": holdings,
    }


def _holding(row: dict[str, Any], value_multiplier: float) -> dict[str, Any]:
    value = _number(row.get("value_usd"))
    if value is not None:
        value *= value_multiplier
    if value is None:
        legacy_value = _number(row.get("value_thousands") or row.get("market_value"))
        value = legacy_value * value_multiplier if legacy_value is not None else None
    shares = _number(row.get("shares_or_principal_amount") or row.get("shares"))
    return {
        "symbol": row.get("symbol") or None,
        "issuer": row.get("name") or row.get("issuer") or None,
        "cusip": row.get("cusip") or None,
        "put_call": row.get("put_call") or None,
        "title": row.get("title") or None,
        "shares": shares,
        "reported_value_usd": value,
        "implied_price": (value / shares)
        if value is not None and shares not in (None, 0)
        else None,
    }


def _aggregate_holdings(holdings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    aggregated: dict[str, dict[str, Any]] = {}
    for holding in holdings:
        key = _holding_key(holding)
        current = aggregated.get(key)
        if current is None:
            aggregated[key] = dict(holding)
            continue
        current["shares"] = (current.get("shares") or 0) + (holding.get("shares") or 0)
        current["reported_value_usd"] = (current.get("reported_value_usd") or 0) + (
            holding.get("reported_value_usd") or 0
        )
        shares = current.get("shares")
        current["implied_price"] = (
            current["reported_value_usd"] / shares if shares else None
        )
    return list(aggregated.values())


def _legacy_value_multiplier(
    details: dict[str, Any], event_date: str | None, holdings: list[Any]
) -> float:
    unit = str(details.get("value_unit") or "").lower()
    if unit in {"usd_native", "usd_converted_from_thousands"}:
        return 1.0
    if unit in {"usd_thousands", "thousands_usd"}:
        return 1000.0
    ratios = []
    for row in holdings:
        if not isinstance(row, dict) or row.get("put_call"):
            continue
        value = _number(
            row.get("value_usd")
            if row.get("value_usd") is not None
            else row.get("value_thousands")
        )
        shares = _number(row.get("shares_or_principal_amount") or row.get("shares"))
        if value and shares and shares > 0:
            ratios.append(value / shares)
    if ratios:
        # Some filers still submit thousands-scaled values in the same XML
        # namespace as nearest-dollar filings. A portfolio-wide sub-dollar
        # implied price identifies the legacy scale.
        return 1000.0 if median(ratios) < 1.0 else 1.0
    # SEC Form 13F changed from thousands to nearest-dollar reporting on
    # 2023-01-03. Use the date only without a usable share sample.
    return 1.0 if str(event_date or "") >= "2023-01-03" else 1000.0


def _changes(
    prior: dict[str, Any] | None, latest: dict[str, Any]
) -> list[dict[str, Any]]:
    before = {_holding_key(item): item for item in (prior or {}).get("holdings", [])}
    after = {_holding_key(item): item for item in latest["holdings"]}
    changes = []
    for key in set(before) | set(after):
        left, right = before.get(key), after.get(key)
        kind = (
            "ADD"
            if left is None
            else "EXIT"
            if right is None
            else "INCREASE"
            if (right["shares"] or 0) > (left["shares"] or 0)
            else "DECREASE"
            if (right["shares"] or 0) < (left["shares"] or 0)
            else "UNCHANGED"
        )
        if kind != "UNCHANGED":
            item = dict(right or left)
            item.update(
                {
                    "change_type": kind,
                    "previous_shares": left and left.get("shares"),
                    "current_shares": right and right.get("shares"),
                    "event_date": latest["event_date"],
                }
            )
            changes.append(item)
    return sorted(
        changes, key=lambda item: item["reported_value_usd"] or 0, reverse=True
    )


def _retained_effect_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    cumulative = 0.0
    available = True
    for index, point in enumerate(history):
        row = dict(point)
        if index == 0:
            row["estimated_retained_share_price_effect_usd"] = 0.0
        else:
            earlier = history[index - 1]
            if any(
                item.get("implied_price") is None or item.get("shares") is None
                for item in (earlier, point)
            ):
                available = False
            if available:
                retained = min(float(earlier["shares"]), float(point["shares"]))
                cumulative += retained * (
                    float(point["implied_price"]) - float(earlier["implied_price"])
                )
                row["estimated_retained_share_price_effect_usd"] = cumulative
            else:
                row["estimated_retained_share_price_effect_usd"] = None
        enriched.append(row)
    return enriched


def _retained_effect(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(history) < 2:
        return None
    value = history[-1].get("estimated_retained_share_price_effect_usd")
    if value is None:
        return None
    return {
        "label": "estimated_retained_share_price_effect",
        "usd": value,
        "method": "Cumulative implied-price change on minimum shares retained between consecutive 13F snapshots.",
        "caveat": METHODOLOGY_CAVEAT,
    }


def _holding_key(item: dict[str, Any]) -> str:
    cusip = str(item.get("cusip") or "").strip().upper()
    option_identity = ":".join(
        str(item.get(key) or "").strip().upper() for key in ("put_call", "title")
    )
    if cusip:
        return f"cusip:{cusip}:{option_identity}"
    symbol = str(item.get("symbol") or "").strip().upper()
    if symbol:
        return f"symbol:{symbol}:{option_identity}"
    return f"issuer:{str(item.get('issuer') or '').strip().upper()}:{option_identity}"


def _canonical_filings(filings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one usable snapshot per report date, preferring the latest amendment."""
    by_report_date: dict[str, dict[str, Any]] = {}
    for filing in filings:
        report_date = str(filing.get("event_date") or filing.get("source_key") or "")
        details = (
            filing.get("details") if isinstance(filing.get("details"), dict) else {}
        )
        holdings = (
            details.get("holdings") if isinstance(details.get("holdings"), list) else []
        )
        rank = (
            bool(holdings),
            str(filing.get("filed_date") or ""),
            str(filing.get("source_key") or ""),
        )
        current = by_report_date.get(report_date)
        if current is None:
            by_report_date[report_date] = filing
            continue
        current_details = (
            current.get("details") if isinstance(current.get("details"), dict) else {}
        )
        current_holdings = (
            current_details.get("holdings")
            if isinstance(current_details.get("holdings"), list)
            else []
        )
        current_rank = (
            bool(current_holdings),
            str(current.get("filed_date") or ""),
            str(current.get("source_key") or ""),
        )
        if rank > current_rank:
            by_report_date[report_date] = filing
    return sorted(
        by_report_date.values(),
        key=lambda item: (
            str(item.get("event_date") or ""),
            str(item.get("filed_date") or ""),
        ),
    )


def _summary(portfolio: dict[str, Any]) -> dict[str, Any]:
    """Keep the scope payload compact; full holdings load on investor selection."""
    return {
        key: value
        for key, value in portfolio.items()
        if key not in {"holdings", "latest_allocation_changes"}
    }


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    return str(value) if value is not None else None
