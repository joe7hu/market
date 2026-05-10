"""Transparent candidate scoring."""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

from investment_panel.core.db import json_dumps, query_rows


def score_symbol(con: Any, symbol: str, weights: dict[str, float]) -> dict[str, Any]:
    technical = latest_technical_score(con, symbol)
    thesis = thesis_score(con, symbol)
    category = category_score(con, symbol)
    trader = trader_score(con, symbol)
    portfolio_fit = portfolio_fit_score(con, symbol)
    fundamental = fundamental_score(con, symbol)
    components = {
        "technical": technical,
        "fundamental": fundamental,
        "category": category,
        "thesis": thesis,
        "trader": trader,
        "portfolio_fit": portfolio_fit,
    }
    score = sum(components[key] * weights.get(key, 0) for key in components)
    gates = gates_for_symbol(con, symbol, components)
    adjusted = apply_gates(score, gates)
    decision = decision_for(adjusted, gates)
    evidence = evidence_for_symbol(con, symbol)
    return {
        "id": stable_id(f"{date.today()}:{symbol}"),
        "run_date": date.today().isoformat(),
        "symbol": symbol,
        "score": round(adjusted, 2),
        "score_breakdown": {
            "components": components,
            "weights": weights,
            "raw_score": round(score, 2),
            "gates": gates,
        },
        "evidence": evidence,
        "decision": decision,
    }


def score_and_store(con: Any, symbols: list[str], weights: dict[str, float]) -> list[dict[str, Any]]:
    rows = [score_symbol(con, symbol, weights) for symbol in symbols]
    for row in rows:
        con.execute(
            """
            INSERT OR REPLACE INTO candidates
            (id, run_date, symbol, score, score_breakdown, evidence, decision)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                row["id"],
                row["run_date"],
                row["symbol"],
                row["score"],
                json_dumps(row["score_breakdown"]),
                json_dumps(row["evidence"]),
                row["decision"],
            ],
        )
    return sorted(rows, key=lambda row: row["score"], reverse=True)


def latest_technical_score(con: Any, symbol: str) -> float:
    rows = query_rows(
        con,
        """
        SELECT features
        FROM technical_features
        WHERE symbol = ?
        ORDER BY date DESC
        LIMIT 1
        """,
        [symbol],
    )
    if not rows:
        return 25.0
    features = parse_json(rows[0]["features"])
    return float(features.get("technical_score", 25.0))


def thesis_score(con: Any, symbol: str) -> float:
    rows = query_rows(con, "SELECT claims FROM birdclaw_theses WHERE symbol = ?", [symbol])
    if not rows:
        return 20.0
    quality = min(len(rows), 6) / 6
    specificity = 0.0
    for row in rows:
        claims = parse_json(row["claims"])
        text = str(claims.get("text", ""))
        if any(term in text.lower() for term in ("why", "because", "margin", "revenue", "cycle", "risk", "invalidation")):
            specificity += 1
    specificity = min(specificity, 4) / 4
    return round(35 + 40 * quality + 25 * specificity, 2)


def category_score(con: Any, symbol: str) -> float:
    rows = query_rows(con, "SELECT category FROM instruments WHERE symbol = ?", [symbol])
    category = (rows[0].get("category") if rows else "") or ""
    if "ai" in category or "crypto" in category or "arco" in category:
        return 65.0
    return 45.0


def trader_score(con: Any, symbol: str) -> float:
    rows = query_rows(con, "SELECT action, filed_date, raw FROM disclosures WHERE symbol = ?", [symbol])
    if not rows:
        return 35.0
    positive = sum(1 for row in rows if str(row.get("action", "")).lower() in {"buy", "add", "new"})
    negative = sum(1 for row in rows if str(row.get("action", "")).lower() in {"sell", "reduce", "exit"})
    return max(0.0, min(100.0, 50 + 12 * positive - 10 * negative))


def portfolio_fit_score(con: Any, symbol: str) -> float:
    owned = query_rows(con, "SELECT symbol FROM portfolio_positions WHERE symbol = ?", [symbol])
    if owned:
        return 48.0
    instrument = query_rows(con, "SELECT category, asset_class FROM instruments WHERE symbol = ?", [symbol])
    if not instrument:
        return 55.0
    category = instrument[0].get("category")
    holdings = query_rows(
        con,
        """
        SELECT i.category
        FROM portfolio_positions p
        JOIN instruments i ON i.symbol = p.symbol
        """,
    )
    overlap = sum(1 for row in holdings if row.get("category") == category)
    return max(25.0, 70.0 - overlap * 12.0)


def fundamental_score(con: Any, symbol: str) -> float:
    equity = query_rows(con, "SELECT metrics FROM equity_fundamentals WHERE symbol = ? ORDER BY period_end DESC LIMIT 1", [symbol])
    crypto = query_rows(con, "SELECT metrics FROM crypto_fundamentals WHERE symbol = ? ORDER BY date DESC LIMIT 1", [symbol])
    if not equity and not crypto:
        return 45.0
    metrics = parse_json((equity or crypto)[0]["metrics"])
    score = 45.0
    for key in ("revenue_growth", "fees_growth", "tvl_growth", "gross_margin_trend", "fcf_margin"):
        if key in metrics:
            score += max(-15, min(18, float(metrics[key]) * 50))
    return max(0.0, min(100.0, score))


def gates_for_symbol(con: Any, symbol: str, components: dict[str, float]) -> list[str]:
    gates: list[str] = []
    price_rows = query_rows(con, "SELECT volume FROM prices_daily WHERE symbol = ? ORDER BY date DESC LIMIT 20", [symbol])
    if len(price_rows) < 20:
        gates.append("data_stale")
    elif sum(float(row.get("volume") or 0) for row in price_rows) / len(price_rows) < 100_000:
        gates.append("liquidity_bad")
    if components["technical"] > 80 and components["thesis"] < 35:
        gates.append("chart_extended_without_thesis")
    if components["portfolio_fit"] < 35:
        gates.append("portfolio_overexposed")
    return gates


def apply_gates(score: float, gates: list[str]) -> float:
    adjusted = score
    penalties = {
        "liquidity_bad": 30,
        "data_stale": 18,
        "chart_extended_without_thesis": 10,
        "portfolio_overexposed": 12,
    }
    for gate in gates:
        adjusted -= penalties.get(gate, 0)
    return max(0.0, adjusted)


def decision_for(score: float, gates: list[str]) -> str:
    if "liquidity_bad" in gates:
        return "reject"
    if score >= 75:
        return "research"
    if score >= 62:
        return "watch"
    return "monitor"


def evidence_for_symbol(con: Any, symbol: str) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT author, created_at, thesis_summary, claims, source_url
        FROM birdclaw_theses
        WHERE symbol = ?
        ORDER BY created_at DESC
        LIMIT 6
        """,
        [symbol],
    )
    evidence = []
    for row in rows:
        evidence.append(
            {
                "type": "arco_thesis",
                "author": row.get("author"),
                "created_at": str(row.get("created_at")),
                "summary": row.get("thesis_summary"),
                "source_url": row.get("source_url"),
                "claims": parse_json(row.get("claims")),
            }
        )
    return evidence


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


def stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]

