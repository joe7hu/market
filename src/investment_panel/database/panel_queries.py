"""Large PostgreSQL panel read-model queries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class QueryPolicy:
    """Everything the loader must know to retrieve one direct read model."""

    query: str
    symbol_scoped: bool = False
    allow_symbol_less: bool = False
    exclude_future_rows: bool = False
    chronological: bool = False
    custom_loader: str | None = None


def build_query_policies(queries: Mapping[str, str]) -> dict[str, QueryPolicy]:
    symbol_scoped = {
        "quotes", "fundamentals", "technicals", "analyst_estimates",
        "options_ticker_signals", "catalysts", "earnings", "research_packets",
    }
    return {
        name: QueryPolicy(
            query=query,
            symbol_scoped=name in symbol_scoped,
            allow_symbol_less=name in {"catalysts", "earnings"},
            exclude_future_rows=name in {"catalysts", "earnings", "research_packets"},
            chronological=name in {"catalysts", "earnings"},
            custom_loader=(
                "options_ticker_signals" if name == "options_ticker_signals"
                else "current_quotes" if name == "quotes"
                else None
            ),
        )
        for name, query in queries.items()
    }

OWNED_CORRELATIONS_QUERY = """
    WITH returns AS (
        SELECT instrument.id, instrument.symbol, bar.trading_date,
               bar.close / lag(bar.close) OVER (
                   PARTITION BY instrument.id ORDER BY bar.trading_date
               ) - 1 AS daily_return
        FROM app.portfolio_position position
        JOIN catalog.instrument instrument ON instrument.id = position.instrument_id
        JOIN raw.price_bar bar ON bar.instrument_id = position.instrument_id
        WHERE bar.interval = '1d' AND bar.trading_date >= current_date - 200
    )
    SELECT left_side.symbol, right_side.symbol AS peer_symbol,
           count(*) AS observations,
           corr(left_side.daily_return, right_side.daily_return) AS correlation
    FROM returns left_side
    JOIN returns right_side
      ON right_side.id > left_side.id
     AND right_side.trading_date = left_side.trading_date
    WHERE left_side.daily_return IS NOT NULL AND right_side.daily_return IS NOT NULL
    GROUP BY left_side.symbol, right_side.symbol
    HAVING count(*) >= 20
    ORDER BY abs(corr(left_side.daily_return, right_side.daily_return)) DESC
"""
