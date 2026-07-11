"""Larger PostgreSQL read-model queries kept outside the panel loader facade."""

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
