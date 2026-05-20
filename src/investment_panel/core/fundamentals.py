"""SEC company-facts fundamentals extraction."""

from __future__ import annotations

from datetime import date
from typing import Any

from investment_panel.core.db import json_dumps
from investment_panel.core.sec import company_facts


US_GAAP = "us-gaap"
REVENUE_TAGS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"]
NET_INCOME_TAGS = ["NetIncomeLoss"]
ASSET_TAGS = ["Assets"]
LIABILITY_TAGS = ["Liabilities"]
CASH_TAGS = ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]


def update_equity_fundamentals(con: Any, instruments: list[dict[str, Any]], user_agent: str) -> int:
    count = 0
    for instrument in instruments:
        cik = instrument.get("cik")
        if not cik or instrument.get("asset_class") not in {"equity", "etf"}:
            continue
        try:
            facts = company_facts(str(cik), user_agent)
            metrics = metrics_from_company_facts(facts)
        except Exception as exc:
            metrics = {"status": "error", "error": str(exc)}
        symbol = instrument["symbol"]
        period_end = metrics.get("period_end") or date.today().isoformat()
        filing_date = metrics.get("filing_date") or date.today().isoformat()
        con.execute(
            """
            INSERT OR REPLACE INTO equity_fundamentals
            (symbol, period_end, filing_date, form_type, metrics, source_url)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                symbol,
                period_end,
                filing_date,
                metrics.get("form_type") or "companyfacts",
                json_dumps(metrics),
                f"https://data.sec.gov/api/xbrl/companyfacts/CIK{str(cik).zfill(10)}.json",
            ],
        )
        count += 1
    return count


def metrics_from_company_facts(payload: dict[str, Any]) -> dict[str, Any]:
    facts = (payload.get("facts") or {}).get(US_GAAP) or {}
    revenue = latest_annual_fact(facts, REVENUE_TAGS)
    revenue_prior = prior_annual_fact(facts, REVENUE_TAGS, revenue)
    net_income = latest_annual_fact(facts, NET_INCOME_TAGS)
    assets = latest_instant_fact(facts, ASSET_TAGS)
    liabilities = latest_instant_fact(facts, LIABILITY_TAGS)
    cash = latest_instant_fact(facts, CASH_TAGS)
    revenue_growth = growth(revenue, revenue_prior)
    net_margin = ratio(net_income, revenue)
    debt_to_assets = ratio(liabilities, assets)
    return {
        "status": "ok",
        "period_end": revenue.get("end") or assets.get("end"),
        "filing_date": revenue.get("filed") or assets.get("filed"),
        "form_type": revenue.get("form") or assets.get("form"),
        "revenue": revenue.get("val"),
        "revenue_prior": revenue_prior.get("val"),
        "revenue_growth": revenue_growth,
        "net_income": net_income.get("val"),
        "net_margin": net_margin,
        "assets": assets.get("val"),
        "liabilities": liabilities.get("val"),
        "cash": cash.get("val"),
        "debt_to_assets": debt_to_assets,
    }


def latest_annual_fact(facts: dict[str, Any], tags: list[str]) -> dict[str, Any]:
    rows = fact_rows(facts, tags)
    annual = [row for row in rows if row.get("fp") == "FY" and row.get("form") in {"10-K", "20-F", "40-F"} and row.get("val") is not None]
    return sorted(annual, key=lambda row: (row.get("end") or "", row.get("filed") or ""))[-1] if annual else {}


def prior_annual_fact(facts: dict[str, Any], tags: list[str], latest: dict[str, Any]) -> dict[str, Any]:
    rows = fact_rows(facts, tags)
    annual = [row for row in rows if row.get("fp") == "FY" and row.get("form") in {"10-K", "20-F", "40-F"} and row.get("val") is not None]
    annual = sorted(annual, key=lambda row: (row.get("end") or "", row.get("filed") or ""))
    if not latest:
        return {}
    for index, row in enumerate(annual):
        if row is latest or (row.get("end") == latest.get("end") and row.get("filed") == latest.get("filed")):
            return annual[index - 1] if index > 0 else {}
    return annual[-2] if len(annual) >= 2 else {}


def latest_instant_fact(facts: dict[str, Any], tags: list[str]) -> dict[str, Any]:
    rows = fact_rows(facts, tags)
    instants = [row for row in rows if row.get("val") is not None and row.get("form") in {"10-K", "10-Q", "20-F", "40-F"}]
    return sorted(instants, key=lambda row: (row.get("end") or "", row.get("filed") or ""))[-1] if instants else {}


def fact_rows(facts: dict[str, Any], tags: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tag in tags:
        units = ((facts.get(tag) or {}).get("units") or {})
        for unit_name in ("USD", "shares", "pure"):
            if unit_name in units:
                rows.extend(list(units[unit_name]))
    return rows


def growth(current: dict[str, Any], prior: dict[str, Any]) -> float | None:
    if not current or not prior or not prior.get("val"):
        return None
    return (float(current["val"]) / float(prior["val"])) - 1


def ratio(numerator: dict[str, Any], denominator: dict[str, Any]) -> float | None:
    if not numerator or not denominator or not denominator.get("val"):
        return None
    return float(numerator["val"]) / float(denominator["val"])
