"""Deterministic valuation models over stored fundamentals, quotes, and screeners."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from investment_panel.core.db import json_dumps, query_rows


@dataclass(frozen=True)
class ValuationContext:
    symbol: str
    price: float
    market_cap: float
    shares_outstanding: float
    revenue: float
    revenue_growth: float
    net_margin: float
    cash: float
    liabilities: float
    category: str | None
    sector: str | None
    source: str


def store_valuation_models(con: Any, symbols: list[str]) -> int:
    today = date.today().isoformat()
    contexts = {context.symbol: context for context in valuation_contexts(con, symbols)}
    peer_multiples = [context.market_cap / context.revenue for context in contexts.values() if context.revenue > 0 and context.market_cap > 0]
    market_median_ev_sales = median(peer_multiples)
    count = 0
    for symbol in symbols:
        con.execute(
            """
            DELETE FROM valuation_models
            WHERE symbol = ?
              AND method IN ('fundamental_proxy', 'dcf_base_case', 'relative_revenue_multiple', 'blended_dcf_relative')
            """,
            [symbol],
        )
        context = contexts.get(symbol)
        if context is None:
            continue
        rows = []
        dcf = dcf_valuation(context)
        if dcf:
            rows.append(dcf)
        relative = relative_valuation(context, market_median_ev_sales)
        if relative:
            rows.append(relative)
        blended = blended_valuation(context, rows)
        if blended:
            rows.append(blended)
        for row in rows:
            con.execute(
                """
                INSERT OR REPLACE INTO valuation_models
                (symbol, as_of, method, fair_value, upside_pct, assumptions, diagnostics)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    symbol,
                    today,
                    row["method"],
                    row["fair_value"],
                    row["upside_pct"],
                    json_dumps(row["assumptions"]),
                    json_dumps(row["diagnostics"]),
                ],
            )
            count += 1
    return count


def valuation_contexts(con: Any, symbols: list[str]) -> list[ValuationContext]:
    output = []
    for symbol in symbols:
        row = valuation_source_row(con, symbol)
        if row is None:
            continue
        metrics = parse_json(row["metrics"])
        price = latest_price(con, symbol)
        market_cap = latest_market_cap(con, symbol)
        revenue = as_float(metrics.get("revenue") or metrics.get("total_revenue"))
        growth = as_float(metrics.get("revenue_growth")) or 0.03
        margin = as_float(metrics.get("net_margin") or metrics.get("profit_margins")) or 0.08
        cash = as_float(metrics.get("cash") or metrics.get("total_cash")) or 0.0
        liabilities = as_float(metrics.get("liabilities") or metrics.get("total_debt")) or 0.0
        if price is None or market_cap is None or revenue is None or price <= 0 or market_cap <= 0 or revenue <= 0:
            continue
        if not metrics_pass_sanity_checks(growth, margin):
            continue
        output.append(
            ValuationContext(
                symbol=symbol,
                price=price,
                market_cap=market_cap,
                shares_outstanding=market_cap / price,
                revenue=revenue,
                revenue_growth=growth,
                net_margin=margin,
                cash=cash,
                liabilities=liabilities,
                category=row.get("category"),
                sector=row.get("sector"),
                source=str(row.get("source") or "fundamentals"),
            )
        )
    return output


def valuation_source_row(con: Any, symbol: str) -> dict[str, Any] | None:
    fundamentals = query_rows(
        con,
        """
        SELECT f.metrics, i.category, i.sector, 'sec_companyfacts' AS source
        FROM equity_fundamentals f
        LEFT JOIN instruments i ON i.symbol = f.symbol
        WHERE f.symbol = ?
        ORDER BY f.period_end DESC
        LIMIT 1
        """,
        [symbol],
    )
    if fundamentals:
        metrics = parse_json(fundamentals[0]["metrics"])
        if metrics.get("status") == "ok":
            return fundamentals[0]
    fallback = query_rows(
        con,
        """
        SELECT m.metrics, i.category, i.sector, m.source
        FROM market_screener_rows m
        LEFT JOIN instruments i ON i.symbol = m.symbol
        WHERE m.symbol = ?
          AND m.source = 'yfinance_info'
        ORDER BY m.observed_at DESC
        LIMIT 1
        """,
        [symbol],
    )
    return fallback[0] if fallback else None


def dcf_valuation(context: ValuationContext) -> dict[str, Any] | None:
    discount_rate = 0.10
    terminal_growth = 0.025
    if discount_rate <= terminal_growth:
        return None
    starting_growth = max(-0.2, min(0.45, context.revenue_growth))
    terminal_margin = max(0.03, min(0.35, context.net_margin + 0.02))
    fcf_conversion = 0.75
    revenue = context.revenue
    projected_fcff = []
    growth_path = []
    for index in range(5):
        growth = starting_growth + (terminal_growth - starting_growth) * (index / 4)
        growth_path.append(growth)
        revenue *= 1 + growth
        margin = context.net_margin + (terminal_margin - context.net_margin) * ((index + 1) / 5)
        projected_fcff.append(revenue * max(0.01, margin) * fcf_conversion)
    terminal_fcff = projected_fcff[-1] * (1 + terminal_growth)
    terminal_value = terminal_fcff / (discount_rate - terminal_growth)
    present_value = sum(value / ((1 + discount_rate) ** (index + 1)) for index, value in enumerate(projected_fcff))
    present_terminal = terminal_value / ((1 + discount_rate) ** 5)
    enterprise_value = present_value + present_terminal
    equity_value = enterprise_value + context.cash - context.liabilities
    if equity_value <= 0:
        return None
    fair_value = equity_value / context.shares_outstanding
    return valuation_row(
        context,
        "dcf_base_case",
        fair_value,
        {
            "discount_rate": discount_rate,
            "terminal_growth": terminal_growth,
            "growth_path": growth_path,
            "terminal_margin": terminal_margin,
            "fcf_conversion": fcf_conversion,
            "net_cash": context.cash - context.liabilities,
        },
        {
            "confidence": "medium_low",
            "method_family": "dcf",
            "terminal_value_pct": present_terminal / enterprise_value if enterprise_value else None,
            "note": f"Five-year FCFF-style DCF from {context.source} fundamentals and latest market cap. Segment/SBC detail requires richer source data.",
        },
    )


def relative_valuation(context: ValuationContext, market_median_ev_sales: float | None) -> dict[str, Any] | None:
    if market_median_ev_sales is None or market_median_ev_sales <= 0:
        return None
    current_multiple = context.market_cap / context.revenue
    target_multiple = max(0.5, min(25.0, market_median_ev_sales))
    if current_multiple <= 0:
        return None
    fair_value = context.price * (target_multiple / current_multiple)
    return valuation_row(
        context,
        "relative_revenue_multiple",
        fair_value,
        {
            "current_ev_sales_proxy": current_multiple,
            "target_ev_sales_proxy": target_multiple,
            "peer_set": "current Market symbols with valid fundamentals and market caps",
        },
        {
            "confidence": "low",
            "method_family": "relative",
            "note": "Revenue multiple comp set is limited to symbols currently loaded in Market.",
        },
    )


def blended_valuation(context: ValuationContext, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    base_rows = [row for row in rows if row.get("method") in {"dcf_base_case", "relative_revenue_multiple"}]
    if len(base_rows) < 2:
        return None
    fair_value = sum(float(row["fair_value"]) for row in base_rows) / len(base_rows)
    return valuation_row(
        context,
        "blended_dcf_relative",
        fair_value,
        {"weights": {row["method"]: round(1 / len(base_rows), 4) for row in base_rows}},
        {
            "confidence": "medium_low",
            "method_family": "triangulated",
            "methods": [row["method"] for row in base_rows],
            "note": "Equal-weight blend of deterministic DCF and relative revenue multiple outputs.",
        },
    )


def valuation_row(
    context: ValuationContext,
    method: str,
    fair_value: float,
    assumptions: dict[str, Any],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "method": method,
        "fair_value": fair_value,
        "upside_pct": ((fair_value - context.price) / context.price) * 100,
        "assumptions": {
            "current_price": context.price,
            "market_cap": context.market_cap,
            "shares_outstanding_proxy": context.shares_outstanding,
            "revenue": context.revenue,
            "revenue_growth": context.revenue_growth,
            "net_margin": context.net_margin,
            **assumptions,
        },
        "diagnostics": diagnostics,
    }


def metrics_pass_sanity_checks(growth: float, margin: float) -> bool:
    """Reject bad fact mappings before they become misleading valuation outputs."""

    return -0.8 <= growth <= 2.0 and -0.75 <= margin <= 0.65


def latest_price(con: Any, symbol: str) -> float | None:
    quote = query_rows(con, "SELECT price FROM quotes_intraday WHERE symbol = ? ORDER BY observed_at DESC LIMIT 1", [symbol])
    if quote and quote[0].get("price") is not None:
        return as_float(quote[0]["price"])
    daily = query_rows(con, "SELECT close FROM prices_daily WHERE symbol = ? ORDER BY date DESC LIMIT 1", [symbol])
    return as_float(daily[0]["close"]) if daily else None


def latest_market_cap(con: Any, symbol: str) -> float | None:
    rows = query_rows(
        con,
        """
        SELECT metrics
        FROM market_screener_rows
        WHERE symbol = ?
        ORDER BY observed_at DESC
        LIMIT 1
        """,
        [symbol],
    )
    if not rows:
        return None
    metrics = parse_json(rows[0].get("metrics"))
    return as_float(
        metrics.get("market_cap_basic")
        or metrics.get("market_cap")
        or metrics.get("market_cap_calc")
        or metrics.get("marketCap")
    )


def median(values: list[float]) -> float | None:
    cleaned = sorted(value for value in values if value > 0)
    if not cleaned:
        return None
    midpoint = len(cleaned) // 2
    if len(cleaned) % 2:
        return cleaned[midpoint]
    return (cleaned[midpoint - 1] + cleaned[midpoint]) / 2


def parse_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    import json

    try:
        return json.loads(value)
    except Exception:
        return {}


def as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
