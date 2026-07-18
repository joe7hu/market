"""PostgreSQL authority for portfolio summary, performance, correlation, and risk."""

from __future__ import annotations

from collections import defaultdict
from contextlib import nullcontext
from datetime import UTC, date, datetime
from itertools import combinations
from math import isfinite, sqrt
from statistics import fmean
from typing import Any
from zoneinfo import ZoneInfo

from investment_panel.database.portfolio_ledger import portfolio_transaction_rows
from investment_panel.database.portfolio_math import adjacent_session_dates, aligned_pair_returns
from investment_panel.database.user_state import portfolio_rows
from investment_panel.database.authority import runtime_for_config


PERFORMANCE_METHOD = "daily-close external-flow adjusted"
CORRELATION_WINDOWS = (20, 60, 120)
MARKET_TIMEZONE = ZoneInfo("America/New_York")


def portfolio_summary(
    config: dict[str, Any],
    *,
    positions: list[dict[str, Any]] | None = None,
    performance: list[dict[str, Any]] | None = None,
    connection: Any | None = None,
) -> dict[str, Any]:
    if connection is None:
        runtime = runtime_for_config(config)
        with runtime.snapshot() as owned_connection:
            return portfolio_summary(config, connection=owned_connection)
    positions = portfolio_rows(config, connection=connection) if positions is None else positions
    accounting = _portfolio_accounting_totals(config, connection=connection)
    performance = portfolio_performance_rows(config, connection=connection) if performance is None else performance
    latest_performance = performance[-1] if performance else {}
    portfolio_value = sum(float(row.get("market_value") or 0) for row in positions)
    cost_basis = sum(float(row.get("quantity") or 0) * float(row.get("avg_cost") or 0) for row in positions)
    net_contributions = accounting["net_contributions"]
    realized_pnl = accounting["realized_pnl"]
    income = accounting["income"]
    fees = accounting["fees"]
    quote_times = [_as_datetime(row.get("quote_observed_at")) for row in positions]
    quote_times = [value for value in quote_times if value is not None]
    fallback_count = sum(row.get("valuation_status") == "cost_basis_fallback" for row in positions)
    total_pnl = portfolio_value - net_contributions
    invested_capital = accounting["invested_capital"]
    prior_performance = performance[-2] if len(performance) > 1 else {}
    adjacent_session = adjacent_session_dates(
        str(prior_performance.get("date") or ""),
        str(latest_performance.get("date") or ""),
        continuous=any(str(row.get("asset_class") or "") == "crypto" for row in positions),
    )
    day_pnl = (
        float(latest_performance.get("total_pnl") or 0) - float(prior_performance.get("total_pnl") or 0)
        if adjacent_session
        else None
    )
    previous_value = float(prior_performance.get("portfolio_value") or 0)
    return {
        "as_of": max(quote_times).isoformat() if quote_times else None,
        "oldest_quote_at": min(quote_times).isoformat() if quote_times else None,
        "portfolio_value": round(portfolio_value, 6),
        "cost_basis": round(cost_basis, 6),
        "net_contributions": round(net_contributions, 6),
        "invested_capital": round(invested_capital, 6),
        "total_pnl": round(total_pnl, 6),
        "total_pnl_pct": round(total_pnl / invested_capital * 100, 6) if invested_capital else None,
        "day_pnl": round(day_pnl, 6) if day_pnl is not None else None,
        "day_pnl_pct": round(day_pnl / previous_value * 100, 6) if day_pnl is not None and previous_value else None,
        "day_pnl_as_of": latest_performance.get("date"),
        "day_pnl_status": "ready" if day_pnl is not None else "insufficient_adjacent_history",
        "realized_pnl": round(realized_pnl, 6),
        "income": round(income, 6),
        "fees": round(fees, 6),
        "holdings_count": len(positions),
        "cost_basis_fallback_count": fallback_count,
        "valuation_status": "cost_basis_fallback" if fallback_count else "market_quotes",
        "currency": "USD",
        "performance_method": PERFORMANCE_METHOD,
    }


def portfolio_performance_rows(
    config: dict[str, Any],
    *,
    connection: Any | None = None,
) -> list[dict[str, Any]]:
    runtime = runtime_for_config(config) if connection is None else None
    with (runtime.read() if runtime is not None else nullcontext(connection)) as connection:
        transactions = [dict(row) for row in connection.execute(
            """
            SELECT transaction.instrument_id, instrument.symbol,
                   transaction.transaction_type, transaction.quantity,
                   transaction.price, transaction.amount, transaction.fees,
                   transaction.executed_at
            FROM app.portfolio_transaction transaction
            LEFT JOIN catalog.instrument instrument ON instrument.id = transaction.instrument_id
            WHERE transaction.reverses_transaction_id IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM app.portfolio_transaction reversal
                  WHERE reversal.reverses_transaction_id = transaction.id
              )
            ORDER BY transaction.executed_at, transaction.created_at
            """
        ).fetchall()]
        if not transactions:
            return []
        instrument_ids = sorted({int(row["instrument_id"]) for row in transactions if row.get("instrument_id") is not None})
        bars = [dict(row) for row in connection.execute(
            """
            SELECT DISTINCT ON (bar.instrument_id, bar.trading_date)
                   bar.instrument_id, instrument.symbol, bar.trading_date, bar.close,
                   ((bar.trading_date::timestamp + time '16:00')
                       AT TIME ZONE 'America/New_York') AS observed_at
            FROM raw.confirmed_price_bar bar
            JOIN catalog.instrument instrument ON instrument.id = bar.instrument_id
            WHERE bar.interval = '1d' AND bar.instrument_id = ANY(%s)
              AND bar.trading_date <= (now() AT TIME ZONE instrument.market_timezone)::date
              AND (bar.observed_at AT TIME ZONE 'UTC')::date = bar.trading_date
              AND bar.available_at <= now()
            ORDER BY bar.instrument_id, bar.trading_date, bar.observed_at DESC, bar.available_at DESC
            """,
            [instrument_ids],
        ).fetchall()] if instrument_ids else []
        current_quotes = [dict(row) for row in connection.execute(
            """
            SELECT DISTINCT ON (quote.instrument_id)
                   quote.instrument_id, instrument.symbol,
                   CASE
                       WHEN quote_source.kind IN ('daily_bars', 'daily_quote')
                       THEN (quote.observed_at AT TIME ZONE 'UTC')::date
                       ELSE (quote.observed_at AT TIME ZONE instrument.market_timezone)::date
                   END AS trading_date,
                   quote.price AS close, quote.observed_at
            FROM raw.confirmed_quote quote
            JOIN catalog.instrument instrument ON instrument.id = quote.instrument_id
            JOIN ingest.source quote_source ON quote_source.id = quote.source_id
            WHERE quote.instrument_id = ANY(%s)
              AND quote.observed_at >= now() - interval '7 days'
              AND quote.available_at <= now()
              AND (
                  quote.observed_at <= now()
                  OR (
                      quote_source.kind IN ('daily_bars', 'daily_quote')
                      AND (quote.observed_at AT TIME ZONE 'UTC')::date
                          <= (now() AT TIME ZONE instrument.market_timezone)::date
                  )
              )
            ORDER BY quote.instrument_id, quote.observed_at DESC, quote.available_at DESC
            """,
            [instrument_ids],
        ).fetchall()] if instrument_ids else []
        benchmark = [dict(row) for row in connection.execute(
            """
            SELECT DISTINCT ON (bar.trading_date) bar.trading_date, bar.close
            FROM raw.confirmed_price_bar bar
            JOIN catalog.instrument instrument ON instrument.id = bar.instrument_id
            WHERE bar.interval = '1d' AND instrument.symbol = 'SPY'
              AND bar.trading_date <= (now() AT TIME ZONE instrument.market_timezone)::date
              AND (bar.observed_at AT TIME ZONE 'UTC')::date = bar.trading_date
              AND bar.available_at <= now()
            ORDER BY bar.trading_date, bar.observed_at DESC, bar.available_at DESC
            """
        ).fetchall()]
    return _performance_rows(transactions, [*bars, *current_quotes], benchmark)


def portfolio_correlation_rows(
    config: dict[str, Any],
    *,
    positions: list[dict[str, Any]] | None = None,
    connection: Any | None = None,
) -> list[dict[str, Any]]:
    positions = portfolio_rows(config, connection=connection) if positions is None else positions
    symbols = sorted(str(row.get("symbol") or "") for row in positions if row.get("symbol"))
    if len(symbols) < 2:
        return []
    weights = {str(row["symbol"]): float(row.get("portfolio_weight") or 0) for row in positions}
    runtime = runtime_for_config(config) if connection is None else None
    with (runtime.read() if runtime is not None else nullcontext(connection)) as connection:
        bars = [dict(row) for row in connection.execute(
            """
            SELECT DISTINCT ON (instrument.symbol, bar.trading_date)
                   instrument.symbol, bar.trading_date, bar.close
            FROM raw.confirmed_price_bar bar
            JOIN catalog.instrument instrument ON instrument.id = bar.instrument_id
            WHERE bar.interval = '1d' AND instrument.symbol = ANY(%s)
              AND bar.trading_date <= (now() AT TIME ZONE instrument.market_timezone)::date
              AND bar.available_at <= now()
            ORDER BY instrument.symbol, bar.trading_date, bar.observed_at DESC, bar.available_at DESC
            """,
            [symbols],
        ).fetchall()]
        split_rows = [dict(row) for row in connection.execute(
            """
            SELECT instrument.symbol,
                   (transaction.executed_at AT TIME ZONE 'America/New_York')::date AS split_date
            FROM app.portfolio_transaction transaction
            JOIN catalog.instrument instrument ON instrument.id = transaction.instrument_id
            WHERE transaction.transaction_type = 'split'
              AND instrument.symbol = ANY(%s)
              AND transaction.reverses_transaction_id IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM app.portfolio_transaction reversal
                  WHERE reversal.reverses_transaction_id = transaction.id
              )
            """,
            [symbols],
        ).fetchall()]
    prices: dict[str, dict[date, float]] = defaultdict(dict)
    for row in bars:
        close = float(row["close"])
        if isfinite(close) and close > 0:
            prices[str(row["symbol"])][row["trading_date"]] = close
    split_dates: dict[str, set[date]] = defaultdict(set)
    for row in split_rows:
        split_dates[str(row["symbol"])].add(row["split_date"])
    output: list[dict[str, Any]] = []
    for symbol, peer_symbol in combinations(symbols, 2):
        interval_dates, left_returns, right_returns = aligned_pair_returns(
            prices.get(symbol, {}),
            prices.get(peer_symbol, {}),
            excluded_dates=split_dates.get(symbol, set()) | split_dates.get(peer_symbol, set()),
        )
        for lookback in CORRELATION_WINDOWS:
            sample_dates = interval_dates[-lookback:]
            left = [left_returns[day] for day in sample_dates]
            right = [right_returns[day] for day in sample_dates]
            correlation = _correlation(left, right) if len(sample_dates) >= min(10, lookback) else None
            combined_weight = weights.get(symbol, 0) + weights.get(peer_symbol, 0)
            risk_level = _correlation_risk(correlation, combined_weight)
            output.append(
                {
                    "edge_id": f"{symbol}:{peer_symbol}:{lookback}",
                    "symbol": symbol,
                    "peer_symbol": peer_symbol,
                    "lookback_days": lookback,
                    "observations": len(sample_dates),
                    "correlation": round(correlation, 6) if correlation is not None else None,
                    "abs_correlation": round(abs(correlation), 6) if correlation is not None else None,
                    "combined_weight": round(combined_weight, 6),
                    "risk_level": risk_level,
                    "as_of": sample_dates[-1].isoformat() if sample_dates else None,
                    "data_status": "ready" if correlation is not None else "insufficient_history",
                    "interpretation": _correlation_interpretation(symbol, peer_symbol, correlation, combined_weight),
                }
            )
    return sorted(
        output,
        key=lambda row: (
            {"critical": 3, "watch": 2, "context": 1, "insufficient": 0}[str(row["risk_level"])],
            int(row["lookback_days"]),
        ),
        reverse=True,
    )


def portfolio_exposure_rows(
    config: dict[str, Any],
    *,
    positions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    positions = portfolio_rows(config) if positions is None else positions
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in positions:
        for cluster_type in ("asset_class", "sector", "industry"):
            cluster_name = str(row.get(cluster_type) or "").strip()
            if cluster_name:
                groups[(cluster_type, cluster_name)].append(row)
    output: list[dict[str, Any]] = []
    for (cluster_type, cluster_name), members in groups.items():
        weight = sum(float(row.get("portfolio_weight") or 0) for row in members)
        symbols = sorted(str(row["symbol"]) for row in members)
        output.append(
            {
                "cluster_id": f"{cluster_type}:{cluster_name}".lower().replace(" ", "-"),
                "cluster_type": cluster_type,
                "cluster_name": cluster_name,
                "symbols": symbols,
                "symbol_count": len(symbols),
                "portfolio_weight": round(weight, 6),
                "largest_symbol": max(members, key=lambda row: float(row.get("portfolio_weight") or 0)).get("symbol"),
                "risk_level": "critical" if weight >= 65 else "watch" if weight >= 35 else "normal",
                "interpretation": f"{weight:.1f}% of current portfolio value sits in {cluster_name}.",
            }
        )
    return sorted(output, key=lambda row: float(row["portfolio_weight"]), reverse=True)


def portfolio_risk_rows(
    config: dict[str, Any],
    *,
    positions: list[dict[str, Any]] | None = None,
    summary: dict[str, Any] | None = None,
    correlations: list[dict[str, Any]] | None = None,
    performance: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    positions = portfolio_rows(config) if positions is None else positions
    summary = portfolio_summary(config, positions=positions) if summary is None else summary
    correlations = portfolio_correlation_rows(config, positions=positions) if correlations is None else correlations
    performance = portfolio_performance_rows(config) if performance is None else performance
    cards: list[dict[str, Any]] = []
    if positions:
        largest = max(positions, key=lambda row: float(row.get("portfolio_weight") or 0))
        weight = float(largest.get("portfolio_weight") or 0)
        if weight >= 35:
            cards.append(
                _risk_card(
                    card_id="largest-position",
                    risk_type="concentration",
                    severity="critical" if weight >= 60 else "watch",
                    title=f"{largest['symbol']} is {weight:.1f}% of the portfolio",
                    summary="One position dominates portfolio outcomes and should have an explicit add, hold, trim, or hedge rule.",
                    symbols=[str(largest["symbol"])],
                    impact=f"{weight:.1f}% of current value",
                    next_step=f"Set the maximum intended weight and first trim threshold for {largest['symbol']}.",
                )
            )
    ready_correlations = [
        row for row in correlations
        if row["lookback_days"] == 60 and float(row.get("correlation") or 0) >= 0.55
    ]
    if ready_correlations:
        edge = ready_correlations[0]
        cards.append(
            _risk_card(
                card_id=f"correlation:{edge['symbol']}:{edge['peer_symbol']}",
                risk_type="correlation",
                severity="critical" if float(edge["combined_weight"]) >= 50 else "watch",
                title=f"{edge['symbol']} and {edge['peer_symbol']} move together",
                summary=str(edge["interpretation"]),
                symbols=[str(edge["symbol"]), str(edge["peer_symbol"])],
                impact=f"{float(edge['combined_weight']):.1f}% combined weight",
                next_step="Give the pair one shared risk budget unless the theses justify independent exposure.",
            )
        )
    current_drawdown = float(performance[-1].get("drawdown_pct") or 0) if performance else 0.0
    if current_drawdown <= -10:
        cards.append(
            _risk_card(
                card_id="portfolio-drawdown",
                risk_type="drawdown",
                severity="critical" if current_drawdown <= -20 else "watch",
                title=f"Portfolio is {abs(current_drawdown):.1f}% below its high-water mark",
                summary="The external-flow-adjusted portfolio path is below its prior high-water mark.",
                symbols=[],
                impact=f"{current_drawdown:.1f}% from peak",
                next_step="Review which positions contributed most before adding risk.",
            )
        )
    quote_times = [_as_datetime(row.get("quote_observed_at")) for row in positions]
    stale_symbols = [
        str(row["symbol"])
        for row, observed in zip(positions, quote_times)
        if observed is None or (datetime.now(UTC) - observed).days > 3
    ]
    if stale_symbols:
        cards.append(
            _risk_card(
                card_id="stale-owned-quotes",
                risk_type="data_freshness",
                severity="watch",
                title=f"{len(stale_symbols)} owned quotes are stale",
                summary="Sizing and P&L should not drive a trade until the owned price set is refreshed.",
                symbols=stale_symbols,
                impact=f"{len(stale_symbols)} of {summary['holdings_count']} holdings",
                next_step="Refresh owned market data, then recompute portfolio risk.",
            )
        )
    return cards


def portfolio_review_action_rows(
    config: dict[str, Any],
    *,
    risk_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return [
        {
            "action_id": f"portfolio:{card['card_id']}",
            "priority": "high" if card["severity"] == "critical" else "medium",
            "status": "open",
            "title": card["title"],
            "symbols": card["symbols"],
            "symbol": card["symbols"][0] if card["symbols"] else None,
            "reason": card["summary"],
            "impact": card["impact"],
            "next_step": card["next_step"],
        }
        for card in (portfolio_risk_rows(config) if risk_rows is None else risk_rows)
    ]


def portfolio_intelligence_tables(config: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    runtime = runtime_for_config(config)
    with runtime.snapshot() as connection:
        positions = portfolio_rows(config, connection=connection)
        performance = portfolio_performance_rows(config, connection=connection)
        transactions = portfolio_transaction_rows(config, connection=connection)
        summary = portfolio_summary(
            config,
            positions=positions,
            performance=performance,
            connection=connection,
        )
        correlations = portfolio_correlation_rows(config, positions=positions, connection=connection)
        exposures = portfolio_exposure_rows(config, positions=positions)
        risks = portfolio_risk_rows(
            config,
            positions=positions,
            summary=summary,
            correlations=correlations,
            performance=performance,
        )
    return {
        "portfolio": positions,
        "portfolio_summary": [summary],
        "portfolio_performance": performance,
        "portfolio_transactions": transactions,
        "correlation_edges": correlations,
        "exposure_clusters": exposures,
        "portfolio_risk_cards": risks,
        "review_actions": portfolio_review_action_rows(config, risk_rows=risks),
    }


def _performance_rows(
    transactions: list[dict[str, Any]],
    bars: list[dict[str, Any]],
    benchmark: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    events_by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in transactions:
        executed_at = _as_datetime(row.get("executed_at"))
        if executed_at:
            events_by_date[executed_at.astimezone(MARKET_TIMEZONE).date()].append(row)
    price_candidates: dict[tuple[date, int], tuple[datetime, float]] = {}
    for row in bars:
        close = float(row["close"])
        if isfinite(close) and close > 0:
            key = (row["trading_date"], int(row["instrument_id"]))
            observed_at = _as_datetime(row.get("observed_at")) or datetime.min.replace(tzinfo=UTC)
            current = price_candidates.get(key)
            if current is None or observed_at >= current[0]:
                price_candidates[key] = (observed_at, close)
    prices_by_date: dict[date, dict[int, float]] = defaultdict(dict)
    price_times_by_date: dict[date, dict[int, datetime]] = defaultdict(dict)
    for (trading_date, instrument_id), (observed_at, close) in price_candidates.items():
        prices_by_date[trading_date][instrument_id] = close
        price_times_by_date[trading_date][instrument_id] = observed_at
    first_transaction_date = min(events_by_date)
    dates = sorted(day for day in set(prices_by_date) | set(events_by_date) if day >= first_transaction_date)
    if not dates:
        return []
    benchmark_by_date = {row["trading_date"]: float(row["close"]) for row in benchmark}
    first_benchmark = next((benchmark_by_date[value] for value in dates if value in benchmark_by_date), None)
    positions: dict[int, float] = defaultdict(float)
    average_costs: dict[int, float] = defaultdict(float)
    last_prices: dict[int, float] = {}
    market_priced: set[int] = set()
    initial_candidates: dict[int, tuple[date, datetime, float]] = {}
    for (trading_date, instrument_id), (observed_at, close) in price_candidates.items():
        if trading_date >= first_transaction_date or (first_transaction_date - trading_date).days > 7:
            continue
        candidate = (trading_date, observed_at, close)
        current = initial_candidates.get(instrument_id)
        if current is None or candidate[:2] > current[:2]:
            initial_candidates[instrument_id] = candidate
    for instrument_id, (_trading_date, _observed_at, close) in initial_candidates.items():
        last_prices[instrument_id] = close
        market_priced.add(instrument_id)
    net_contributions = 0.0
    invested_capital = 0.0
    previous_value = 0.0
    twr_index = 1.0
    twr_peak = 1.0
    rows: list[dict[str, Any]] = []

    for current_date in dates:
        current_prices = prices_by_date.get(current_date, {})
        last_prices.update(current_prices)
        market_priced.update(current_prices)
        daily_flow = 0.0
        for event in events_by_date.get(current_date, []):
            instrument_id = event.get("instrument_id")
            transaction_type = str(event.get("transaction_type") or "")
            quantity = float(event.get("quantity") or 0)
            price = float(event.get("price") or 0)
            if instrument_id is not None:
                instrument_id = int(instrument_id)
                if transaction_type in {"opening_balance", "buy", "transfer_in"}:
                    old_quantity = positions[instrument_id]
                    new_quantity = old_quantity + quantity
                    average_costs[instrument_id] = (
                        (old_quantity * average_costs[instrument_id] + quantity * price + float(event.get("fees") or 0))
                        / new_quantity
                    ) if new_quantity else 0.0
                    positions[instrument_id] = new_quantity
                    if instrument_id not in market_priced:
                        last_prices[instrument_id] = average_costs[instrument_id]
                elif transaction_type in {"sell", "transfer_out"}:
                    last_prices.setdefault(instrument_id, price)
                    positions[instrument_id] -= quantity
                    if positions[instrument_id] <= 0:
                        average_costs[instrument_id] = 0.0
                elif transaction_type == "split":
                    positions[instrument_id] *= quantity
                    average_costs[instrument_id] /= quantity
                    price_observed_at = price_times_by_date.get(current_date, {}).get(instrument_id)
                    split_at = _as_datetime(event.get("executed_at"))
                    if (
                        last_prices.get(instrument_id)
                        and (price_observed_at is None or split_at is None or price_observed_at < split_at)
                    ):
                        last_prices[instrument_id] = average_costs[instrument_id]
                        market_priced.discard(instrument_id)
            flow = _transaction_flow(event)
            net_contributions += flow
            invested_capital += _invested_capital_flow(event)
            daily_flow += flow
        portfolio_value = sum(quantity * last_prices.get(instrument_id, 0) for instrument_id, quantity in positions.items())
        daily_return = (portfolio_value - daily_flow) / previous_value - 1 if previous_value else 0.0
        twr_index *= 1 + daily_return
        twr_peak = max(twr_peak, twr_index)
        benchmark_value = benchmark_by_date.get(current_date)
        rows.append(
            {
                "date": current_date.isoformat(),
                "portfolio_value": round(portfolio_value, 6),
                "net_contributions": round(net_contributions, 6),
                "invested_capital": round(invested_capital, 6),
                "total_pnl": round(portfolio_value - net_contributions, 6),
                "total_return_pct": round((portfolio_value - net_contributions) / invested_capital * 100, 6)
                if invested_capital
                else None,
                "time_weighted_return_pct": round((twr_index - 1) * 100, 6),
                "drawdown_pct": round((twr_index / twr_peak - 1) * 100, 6),
                "benchmark_return_pct": round((benchmark_value / first_benchmark - 1) * 100, 6)
                if benchmark_value is not None and first_benchmark
                else None,
            }
        )
        previous_value = portfolio_value
    return rows


def _transaction_flow(row: dict[str, Any]) -> float:
    transaction_type = str(row.get("transaction_type") or "")
    amount = float(row.get("amount") or 0)
    fees = float(row.get("fees") or 0)
    if transaction_type in {"opening_balance", "buy", "transfer_in", "cash_deposit"}:
        return amount + fees
    if transaction_type in {"sell", "transfer_out", "cash_withdrawal"}:
        return -(amount - fees)
    if transaction_type == "dividend":
        return -(amount - fees)
    if transaction_type == "fee":
        return amount + fees
    if transaction_type == "split":
        return fees
    return 0.0


def _portfolio_accounting_totals(
    config: dict[str, Any],
    *,
    connection: Any | None = None,
) -> dict[str, float]:
    runtime = runtime_for_config(config) if connection is None else None
    with (runtime.read() if runtime is not None else nullcontext(connection)) as connection:
        row = connection.execute(
            """
            SELECT
                COALESCE(sum(CASE
                    WHEN transaction_type IN ('opening_balance', 'buy', 'transfer_in') THEN amount + fees
                    WHEN transaction_type IN ('sell', 'transfer_out') THEN -(amount - fees)
                    WHEN transaction_type = 'dividend' THEN -(amount - fees)
                    WHEN transaction_type = 'fee' THEN amount + fees
                    WHEN transaction_type = 'split' THEN fees
                    ELSE 0 END), 0) AS net_contributions,
                COALESCE(sum(CASE
                    WHEN transaction_type IN ('opening_balance', 'buy', 'transfer_in') THEN amount + fees
                    ELSE 0 END), 0) AS invested_capital,
                COALESCE(sum(realized_pnl), 0) AS realized_pnl,
                COALESCE(sum(amount) FILTER (WHERE transaction_type = 'dividend'), 0) AS income,
                COALESCE(sum(fees), 0)
                    + COALESCE(sum(amount) FILTER (WHERE transaction_type = 'fee'), 0) AS fees
            FROM app.portfolio_transaction transaction
            WHERE transaction.reverses_transaction_id IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM app.portfolio_transaction reversal
                  WHERE reversal.reverses_transaction_id = transaction.id
              )
            """
        ).fetchone()
    return {key: float(row[key] or 0) for key in ("net_contributions", "invested_capital", "realized_pnl", "income", "fees")}


def _invested_capital_flow(row: dict[str, Any]) -> float:
    if str(row.get("transaction_type") or "") not in {"opening_balance", "buy", "transfer_in"}:
        return 0.0
    return float(row.get("amount") or 0) + float(row.get("fees") or 0)


def _correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = fmean(left)
    right_mean = fmean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    left_variance = sum((x - left_mean) ** 2 for x in left)
    right_variance = sum((y - right_mean) ** 2 for y in right)
    denominator = sqrt(left_variance * right_variance)
    return numerator / denominator if denominator else None


def _correlation_risk(correlation: float | None, combined_weight: float) -> str:
    if correlation is None:
        return "insufficient"
    if correlation >= 0.75 and combined_weight >= 35:
        return "critical"
    if correlation >= 0.55:
        return "watch"
    return "context"


def _correlation_interpretation(
    symbol: str, peer_symbol: str, correlation: float | None, combined_weight: float,
) -> str:
    if correlation is None:
        return f"Not enough overlapping daily history to judge {symbol} and {peer_symbol}."
    if correlation >= 0.75:
        return f"{symbol} and {peer_symbol} move together closely and represent {combined_weight:.1f}% combined exposure."
    if correlation >= 0.55:
        return f"{symbol} and {peer_symbol} often move together; size them with a shared risk budget."
    if correlation <= -0.55:
        return f"{symbol} and {peer_symbol} have moved in opposite directions, but that hedge relationship can change by regime."
    return f"{symbol} and {peer_symbol} have low recent co-movement; correlation is context, not proof of diversification."


def _risk_card(*, card_id: str, risk_type: str, severity: str, title: str, summary: str,
    symbols: list[str], impact: str, next_step: str) -> dict[str, Any]:
    return {
        "card_id": card_id,
        "risk_type": risk_type,
        "severity": severity,
        "score": {"critical": 90, "watch": 65, "info": 40}.get(severity, 50),
        "title": title,
        "summary": summary,
        "symbols": symbols,
        "symbol": symbols[0] if symbols else None,
        "impact": impact,
        "next_step": next_step,
    }


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
