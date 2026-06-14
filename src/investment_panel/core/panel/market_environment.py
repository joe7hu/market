"""Market context, valuation, and environment read models.

DB-backed accessors (signature ``con -> list[dict]``) live here; the pure
scoring/summary/aggregate computations they feed live in
``investment_panel.analysis.market_environment_scoring``.
"""

from __future__ import annotations
from typing import Any
from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.decision import canonical_quote_rows, decision_readiness_rows, effective_watchlist, manual_watchlist_rows, refresh_decision_read_models
from investment_panel.core.portfolio_intelligence import correlation_edges, exposure_clusters, portfolio_risk_cards, review_actions

from investment_panel.core.panel.coerce import _average, _dict_from_value, _format_metric, _last_history_close, _median, _normalize_symbol_token, _optional_number, _percentile_rank, _share, _symbols_from_value
from investment_panel.core.panel.metrics import _is_watch_universe, _metric_number, _ps_from_fundamentals
from investment_panel.core.panel.technicals import technical_price_history, technicals
from investment_panel.core.panel.disclosures import _compact_empty_fields
from investment_panel.core.panel.read_equity import candidates, discovered_universe, portfolio
from investment_panel.core.panel.read_market_data import earnings_setups, liquidity, quotes, screener, valuations
from investment_panel.core.panel.read_tradingview import tradingview_watchlists

from investment_panel.analysis.market_environment_scoring import (
    _aggregate_normalized_history,
    _asset_breadth_score,
    _asset_breadth_summary,
    _asset_trend_score,
    _asset_trend_summary,
    _environment_bucket,
    _environment_posture,
    _leadership_score,
    _leadership_summary,
    _liquidity_score,
    _market_symbol_scope,
    _market_valuation_aggregate,
    _market_valuation_reference_source,
    _market_valuation_reference_summary,
    _overall_environment_summary,
    _overall_next_action,
    _overall_portfolio_effect,
    _portfolio_environment_score,
    _preferred_valuation_by_symbol,
    _price_overlay_by_month,
    _risk_appetite_score,
    _risk_appetite_summary,
    _sample_market_metric_history,
    _valuation_chart_points,
    _valuation_coverage,
    _valuation_next_action,
    _valuation_posture,
    _valuation_score,
    _weighted_environment_score,
)



def market_context(con: Any) -> list[dict[str, Any]]:
    """Macro/market posture only when it affects sizing or portfolio risk."""

    rows: list[dict[str, Any]] = []
    for cluster in exposure_clusters(con)[:8]:
        rows.append(
            {
                "metric": f"{cluster.get('cluster_name') or 'Exposure'} concentration",
                "latest_value": cluster.get("portfolio_weight"),
                "unit": "%",
                "date": cluster.get("as_of"),
                "percentile": None,
                "posture": cluster.get("concentration_level") or "watch",
                "portfolio_effect": cluster.get("risk_readout") or cluster.get("next_step") or "Review sizing only if concentration changed.",
                "history": [],
            }
        )
    for card in portfolio_risk_cards(con)[:8]:
        rows.append(
            {
                "metric": card.get("title") or card.get("risk_type") or "Portfolio risk",
                "latest_value": card.get("score"),
                "unit": "score",
                "date": card.get("as_of"),
                "percentile": None,
                "posture": card.get("severity") or "watch",
                "portfolio_effect": card.get("impact") or card.get("next_step") or card.get("summary"),
                "history": [],
            }
        )
    if not rows:
        rows.append(
            {
                "metric": "Position sizing posture",
                "latest_value": None,
                "unit": "",
                "date": None,
                "percentile": None,
                "posture": "neutral",
                "portfolio_effect": "No macro or portfolio-risk row currently changes sizing.",
                "history": [],
            }
        )
    return [_compact_empty_fields(row) for row in rows[:12]]




def market_valuation_reference_charts(con: Any) -> list[dict[str, Any]]:
    """Broad-market valuation series with latest percentile context."""

    rows = query_rows(
        con,
        """
        SELECT metric, as_of, label, value, suffix, higher_is_better, source, source_url
        FROM market_valuation_metric_points
        WHERE metric IN ('sp500_forward_pe', 'shiller_pe', 'sp500_pe', 'equity_risk_premium', 'sp500_price')
        ORDER BY metric, as_of
        """,
    )
    by_metric: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_metric.setdefault(str(row.get("metric") or ""), []).append(row)
    price_by_month = _price_overlay_by_month(by_metric.get("sp500_price") or [])
    output = []
    for metric in ("sp500_forward_pe", "shiller_pe", "sp500_pe", "equity_risk_premium"):
        points = by_metric.get(metric) or []
        if not points:
            continue
        latest = points[-1]
        values = [_optional_number(point.get("value")) for point in points]
        latest_value = _optional_number(latest.get("value"))
        percentile = _percentile_rank(values, latest_value)
        higher_is_better = bool(latest.get("higher_is_better"))
        score = percentile if higher_is_better else (100 - percentile if percentile is not None else None)
        output.append(
            _compact_empty_fields(
                {
                    "metric": metric,
                    "label": latest.get("label") or metric,
                    "latest_value": latest_value,
                    "latest_date": latest.get("as_of"),
                    "percentile": percentile,
                    "score": score,
                    "suffix": latest.get("suffix"),
                    "higher_is_better": higher_is_better,
                    "posture": _environment_posture(score),
                    "source": latest.get("source"),
                    "source_url": latest.get("source_url"),
                    "history": _sample_market_metric_history(points, price_by_month),
                }
            )
        )
    return output




def market_environment_assets(con: Any) -> list[dict[str, Any]]:
    """Latest broad-market asset rows used by the environment model."""

    return query_rows(
        con,
        """
        SELECT symbol, as_of, group_name, name, price, return_1d, return_ytd, return_1w,
               return_1m, return_1y, pct_from_52w_high, sma_10_up, sma_20_up,
               sma_50_up, sma_200_up, sma_20_gt_50, sma_50_gt_200, range_ratio_52w,
               color,
               CASE
                 WHEN source = 'fullstack_market_model_sheet' THEN 'market_environment_asset_matrix'
                 ELSE source
               END AS source
        FROM market_environment_asset_snapshots
        WHERE as_of = (SELECT max(as_of) FROM market_environment_asset_snapshots)
        ORDER BY
          CASE group_name
            WHEN 'Market' THEN 0
            WHEN 'Sectors' THEN 1
            WHEN 'Industries' THEN 2
            WHEN 'Managed ETFs' THEN 3
            WHEN 'Countries' THEN 4
            WHEN 'Others' THEN 5
            WHEN 'Macro' THEN 6
            ELSE 7
          END,
          symbol
        """
    )




class MarketDisplayContext:
    """Cached market display rows for one panel load."""

    def __init__(self, con: Any, symbols: list[str]) -> None:
        self.con = con
        self.symbols = sorted({str(symbol or "").upper() for symbol in symbols if symbol})
        self._histories: dict[str, list[dict[str, Any]]] | None = None
        self._quotes: dict[str, dict[str, Any]] | None = None
        self._screener: dict[str, dict[str, Any]] | None = None
        self._technicals: dict[str, dict[str, Any]] | None = None
        self._valuations: dict[str, dict[str, Any]] | None = None

    @property
    def histories(self) -> dict[str, list[dict[str, Any]]]:
        if self._histories is None:
            self._histories = technical_price_history(self.con, self.symbols, days=253)
        return self._histories

    @property
    def quotes_by_symbol(self) -> dict[str, dict[str, Any]]:
        if self._quotes is None:
            self._quotes = {str(row.get("symbol") or "").upper(): row for row in quotes(self.con) if str(row.get("symbol") or "").upper() in self.symbols}
        return self._quotes

    @property
    def screener_by_symbol(self) -> dict[str, dict[str, Any]]:
        if self._screener is None:
            self._screener = {str(row.get("symbol") or "").upper(): row for row in screener(self.con) if str(row.get("symbol") or "").upper() in self.symbols}
        return self._screener

    @property
    def technicals_by_symbol(self) -> dict[str, dict[str, Any]]:
        if self._technicals is None:
            self._technicals = {
                str(row.get("symbol") or "").upper(): row
                for row in technicals(self.con, symbols=self.symbols, price_history=self.histories)
            }
        return self._technicals

    @property
    def valuations_by_symbol(self) -> dict[str, dict[str, Any]]:
        if self._valuations is None:
            self._valuations = _preferred_valuation_by_symbol([row for row in valuations(self.con) if str(row.get("symbol") or "").upper() in self.symbols])
        return self._valuations




def market_display_context(con: Any, config_watchlist: list[dict[str, Any]] | None = None) -> MarketDisplayContext:
    return MarketDisplayContext(con, _market_stance_symbols(con, config_watchlist))




def market_valuation_charts(
    con: Any,
    config_watchlist: list[dict[str, Any]] | None = None,
    context: MarketDisplayContext | None = None,
) -> list[dict[str, Any]]:
    """Watchlist and whole-market valuation chart rows for the Market page."""

    display_context = context or market_display_context(con, config_watchlist)
    symbols = display_context.symbols
    histories = display_context.histories
    quote_by_symbol = display_context.quotes_by_symbol
    screener_by_symbol = display_context.screener_by_symbol
    technical_by_symbol = display_context.technicals_by_symbol
    valuation_by_symbol = display_context.valuations_by_symbol
    rows: list[dict[str, Any]] = []

    for symbol in symbols:
        metrics = _dict_from_value(screener_by_symbol.get(symbol, {}).get("metrics"))
        valuation = valuation_by_symbol.get(symbol, {})
        quote = quote_by_symbol.get(symbol, {})
        history = histories.get(symbol, [])
        latest_price = _optional_number(quote.get("price")) or _last_history_close(history)
        fair_value = _optional_number(valuation.get("fair_value"))
        upside = _optional_number(valuation.get("upside_pct"))
        forward_pe = _metric_number(metrics, "forward_pe", "forwardPE", "forward_pe_ratio", "pe_forward", "trailingPE", "trailing_pe")
        ps_ratio = _ps_from_fundamentals(metrics, {})
        market_cap = _metric_number(metrics, "market_cap", "marketCap", "market_cap_basic", "market_capitalization")
        chart_points = _valuation_chart_points(history, latest_price, fair_value)
        rows.append(
            _compact_empty_fields(
                {
                    "symbol": symbol,
                    "name": screener_by_symbol.get(symbol, {}).get("name") or symbol,
                    "scope": _market_symbol_scope(symbol, quote, config_watchlist),
                    "latest_price": latest_price,
                    "change_pct": quote.get("change_pct"),
                    "fair_value": fair_value,
                    "upside_pct": upside,
                    "forward_pe": forward_pe,
                    "ps_ratio": ps_ratio,
                    "market_cap": market_cap,
                    "valuation_posture": _valuation_posture(upside, forward_pe, ps_ratio),
                    "valuation_score": _valuation_score(upside, forward_pe, ps_ratio),
                    "technical_score": technical_by_symbol.get(symbol, {}).get("technical_score"),
                    "return_60d": technical_by_symbol.get(symbol, {}).get("return_60d"),
                    "method": valuation.get("method"),
                    "confidence": _dict_from_value(valuation.get("diagnostics")).get("confidence"),
                    "source": valuation.get("method") or screener_by_symbol.get(symbol, {}).get("source") or quote.get("source"),
                    "history": chart_points,
                    "coverage": _valuation_coverage(latest_price, fair_value, forward_pe, ps_ratio, history),
                    "next_action": _valuation_next_action(symbol, upside, forward_pe, ps_ratio),
                }
            )
        )

    aggregate = _market_valuation_aggregate(rows)
    return [aggregate, *rows] if aggregate else rows




def market_environment_model(con: Any, config_watchlist: list[dict[str, Any]] | None = None, include_exposure: bool = True) -> list[dict[str, Any]]:
    """Deterministic market environment model for sizing posture."""

    valuation_reference_rows = market_valuation_reference_charts(con)
    asset_rows = market_environment_assets(con)
    needs_watchlist_fallback = not valuation_reference_rows or not asset_rows
    display_context = market_display_context(con, config_watchlist) if include_exposure or needs_watchlist_fallback else None
    valuation_rows = market_valuation_charts(con, config_watchlist, context=display_context) if display_context else []
    ticker_rows = [row for row in valuation_rows if row.get("scope") != "whole_market"]
    stance_symbols = display_context.symbols if display_context else []
    technical_rows = list(display_context.technicals_by_symbol.values()) if display_context else []
    liquidity_rows = [row for row in liquidity(con) if str(row.get("symbol") or "").upper() in stance_symbols] if include_exposure and stance_symbols else []
    risk_rows = portfolio_risk_cards(con) if include_exposure else []
    correlation_rows = correlation_edges(con) if include_exposure else []
    earnings_rows = [row for row in earnings_setups(con) if str(row.get("symbol") or "").upper() in stance_symbols] if include_exposure and stance_symbols else []
    technical_scores = [score for score in (_optional_number(row.get("technical_score")) for row in technical_rows) if score is not None]
    breadth_score = (_share([score >= 55 for score in technical_scores]) * 100) if technical_scores else None
    broad_valuation_score = _average([_optional_number(row.get("score")) for row in valuation_reference_rows])
    watchlist_valuation_score = _average([_optional_number(row.get("valuation_score")) for row in ticker_rows])
    valuation_score = broad_valuation_score if broad_valuation_score is not None else watchlist_valuation_score
    valuation_evidence = _market_valuation_reference_summary(valuation_reference_rows) or f"{_format_metric(_median([_optional_number(row.get('forward_pe')) for row in ticker_rows]), 'x')} median forward P/E; {_format_metric(_median([_optional_number(row.get('upside_pct')) for row in ticker_rows]), '%')} median fair-value gap."
    asset_trend_score = _asset_trend_score(asset_rows)
    market_trend_score = asset_trend_score if asset_trend_score is not None else _average([_optional_number(row.get("technical_score")) for row in technical_rows])
    asset_breadth_score = _asset_breadth_score(asset_rows)
    market_breadth_score = asset_breadth_score if asset_breadth_score is not None else breadth_score
    risk_appetite_score = _risk_appetite_score(asset_rows)
    leadership_score = _leadership_score(asset_rows)
    valuation_source = _market_valuation_reference_source(valuation_reference_rows) if valuation_reference_rows else "Watchlist valuation models"
    market_asset_source = "Market environment asset matrix" if asset_rows else "Not loaded"

    buckets = [
        _environment_bucket(
            "Valuation",
            valuation_score,
            valuation_evidence,
            "Lean into new risk only when discounts compensate for thesis risk.",
            "Use broad-market valuation percentiles before increasing beta exposure.",
            weight=0.25,
            source=valuation_source,
        ),
        _environment_bucket(
            "Price Trend",
            market_trend_score,
            _asset_trend_summary(asset_rows) or f"{_format_metric(_average([_optional_number(row.get('return_60d')) * 100 for row in technical_rows if _optional_number(row.get('return_60d')) is not None]), '%')} average 60-day return across covered watchlist names.",
            "Positive trend supports normal sizing; weak trend argues for staged entries.",
            "Check whether broad indices and sectors remain above key moving averages.",
            weight=0.20,
            source=market_asset_source if asset_rows else "Watchlist technicals",
        ),
        _environment_bucket(
            "Market Breadth",
            market_breadth_score,
            _asset_breadth_summary(asset_rows) or f"{_format_metric(breadth_score, '%')} of covered names have constructive technical scores.",
            "Narrow breadth raises single-name selection risk.",
            "Prefer source-backed names only when breadth is not deteriorating.",
            weight=0.20,
            source=market_asset_source if asset_rows else "Watchlist technicals",
        ),
        _environment_bucket(
            "Risk Appetite",
            risk_appetite_score,
            _risk_appetite_summary(asset_rows),
            "Volatility, dollar, bonds, and crypto risk appetite change timing and cash posture.",
            "Reduce chase risk when volatility or macro pressure rises.",
            weight=0.15,
            source=market_asset_source,
        ),
        _environment_bucket(
            "Sector / Theme Leadership",
            leadership_score,
            _leadership_summary(asset_rows),
            "Sector and theme leadership shows whether risk is broadening or crowded.",
            "Favor leaders with breadth confirmation; fade crowded laggards.",
            weight=0.10,
            source=market_asset_source,
        ),
    ]
    if include_exposure:
        buckets.extend(
            [
                _environment_bucket(
                    "Liquidity",
                    _liquidity_score(liquidity_rows),
                    f"{len(liquidity_rows)} liquidity rows loaded; {_format_metric(_median([_optional_number(row.get('avg_dollar_volume')) for row in liquidity_rows]), '$')} median dollar volume.",
                    "Thin liquidity should cap position size even when thesis quality is high.",
                    "Avoid chasing low-volume watchlist names without a limit plan.",
                    weight=0.05,
                    source="Watchlist liquidity metrics",
                ),
                _environment_bucket(
                    "Portfolio Risk",
                    _portfolio_environment_score(risk_rows, correlation_rows),
                    f"{len(risk_rows)} portfolio risk cards and {len(correlation_rows)} major correlation edges currently affect the model.",
                    "Risk model overrides market optimism when concentration or correlation is elevated.",
                    "Review the highest-severity risk card before adding exposure.",
                    weight=0.05,
                    source="Portfolio risk model",
                ),
                _environment_bucket(
                    "Earnings Setup",
                    _average([_optional_number(row.get("score")) for row in earnings_rows]),
                    f"{len(earnings_rows)} earnings setup rows loaded for watched names.",
                    "Event risk should change timing more than thesis conviction.",
                    "Stage entries around high-score setups only when valuation is not stretched.",
                    weight=0.05,
                    source="Watchlist earnings setup",
                ),
            ]
        )
    scored = [bucket for bucket in buckets if bucket.get("score") is not None]
    overall_score = _weighted_environment_score(scored)
    overall = _environment_bucket(
        "Overall",
        overall_score,
        _overall_environment_summary(scored),
        _overall_portfolio_effect(overall_score),
        _overall_next_action(overall_score),
        weight=1.0,
        source="Weighted environment model",
    )
    return [_compact_empty_fields(row) for row in [overall, *buckets]]




def _market_stance_symbols(con: Any, config_watchlist: list[dict[str, Any]] | None = None) -> list[str]:
    symbols: list[str] = []
    for item in config_watchlist or []:
        symbols.append(_normalize_symbol_token(item.get("symbol")))
    for row in manual_watchlist_rows(con, include_excluded=False):
        symbols.append(_normalize_symbol_token(row.get("symbol")))
    for row in portfolio(con):
        symbols.append(_normalize_symbol_token(row.get("symbol")))
    for row in tradingview_watchlists(con):
        symbols.extend(_symbols_from_value(row.get("symbols")))
    for row in discovered_universe(con):
        if _is_watch_universe(row):
            symbols.append(_normalize_symbol_token(row.get("symbol")))
    for benchmark in ("SPY", "QQQ", "IWM"):
        symbols.append(benchmark)
    return sorted({symbol for symbol in symbols if symbol})
