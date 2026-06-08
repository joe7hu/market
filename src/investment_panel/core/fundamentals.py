"""SEC company-facts fundamentals extraction."""

from __future__ import annotations

from datetime import date
import re
from typing import Any

from investment_panel.core.db import json_dumps
from investment_panel.core.sec import company_facts, company_tickers
from investment_panel.providers.yfinance_provider import YFinanceProvider, YFinanceUnavailable


US_GAAP = "us-gaap"
REVENUE_TAGS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"]
NET_INCOME_TAGS = ["NetIncomeLoss"]
ASSET_TAGS = ["Assets"]
LIABILITY_TAGS = ["Liabilities"]
CASH_TAGS = ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]
OPERATING_CASH_FLOW_TAGS = ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]
CAPEX_TAGS = ["PaymentsToAcquirePropertyPlantAndEquipment", "CapitalExpenditures"]
DEFAULT_CIK_BY_SYMBOL = {
    "AAPL": "0000320193",
    "AMZN": "0001018724",
    "GOOG": "0001652044",
    "GOOGL": "0001652044",
    "META": "0001326801",
    "MSFT": "0000789019",
    "NFLX": "0001065280",
    "NVDA": "0001045810",
    "TSLA": "0001318605",
}


def update_equity_fundamentals(con: Any, instruments: list[dict[str, Any]], user_agent: str) -> int:
    cik_by_symbol = company_cik_map(user_agent)
    yfinance_provider: YFinanceProvider | None = None
    count = 0
    for instrument in instruments:
        symbol = str(instrument.get("symbol") or "").upper()
        cik = instrument.get("cik") or cik_by_symbol.get(symbol)
        if not cik or instrument.get("asset_class") not in {"equity", "etf"}:
            if instrument.get("asset_class") not in {"equity", "etf"}:
                continue
            if yfinance_provider is None:
                try:
                    yfinance_provider = YFinanceProvider()
                except YFinanceUnavailable:
                    continue
            cik = cik_from_yfinance_filings(symbol, yfinance_provider)
            if not cik:
                continue
        try:
            facts = company_facts(str(cik), user_agent)
            metrics = metrics_from_company_facts(facts)
        except Exception:
            continue
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


def company_cik_map(user_agent: str) -> dict[str, str]:
    output: dict[str, str] = dict(DEFAULT_CIK_BY_SYMBOL)
    try:
        rows = company_tickers(user_agent)
    except Exception:
        return output
    for row in rows.values() if isinstance(rows, dict) else []:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").upper()
        cik = row.get("cik_str")
        if ticker and cik:
            output[ticker] = str(cik).zfill(10)
    return output


def cik_from_yfinance_filings(symbol: str, provider: YFinanceProvider | None = None) -> str | None:
    provider = provider or YFinanceProvider()
    try:
        filings = provider.sec_filings(symbol)
    except Exception:
        return None
    return cik_from_sec_filings(filings)


def cik_from_sec_filings(filings: Any) -> str | None:
    rows = filings if isinstance(filings, list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        candidates = [row.get("edgarUrl")]
        exhibits = row.get("exhibits")
        if isinstance(exhibits, dict):
            candidates.extend(exhibits.values())
        for value in candidates:
            cik = cik_from_sec_filing_url(str(value or ""))
            if cik:
                return cik
    return None


def cik_from_sec_filing_url(url: str) -> str | None:
    match = re.search(r"/sec-filings/0*(\d{1,10})(?:/|$)", url)
    if not match:
        match = re.search(r"_(\d{1,10})(?:[/?#]|$)", url)
    return match.group(1).zfill(10) if match else None


def metrics_from_company_facts(payload: dict[str, Any]) -> dict[str, Any]:
    facts = (payload.get("facts") or {}).get(US_GAAP) or {}
    revenue = latest_annual_fact(facts, REVENUE_TAGS)
    revenue_prior = prior_annual_fact(facts, REVENUE_TAGS, revenue)
    net_income = latest_annual_fact(facts, NET_INCOME_TAGS)
    assets = latest_instant_fact(facts, ASSET_TAGS)
    liabilities = latest_instant_fact(facts, LIABILITY_TAGS)
    cash = latest_instant_fact(facts, CASH_TAGS)
    operating_cash_flow = latest_annual_fact(facts, OPERATING_CASH_FLOW_TAGS)
    capital_expenditures = latest_annual_fact(facts, CAPEX_TAGS)
    revenue_growth = growth(revenue, revenue_prior)
    net_margin = ratio(net_income, revenue)
    debt_to_assets = ratio(liabilities, assets)
    free_cash_flow = cash_flow_after_capex(operating_cash_flow, capital_expenditures)
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
        "operating_cash_flow": operating_cash_flow.get("val"),
        "capital_expenditures": capital_expenditures.get("val"),
        "free_cash_flow": free_cash_flow,
        "fcf_margin": ratio({"val": free_cash_flow}, revenue) if free_cash_flow is not None else None,
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


def cash_flow_after_capex(operating_cash_flow: dict[str, Any], capital_expenditures: dict[str, Any]) -> float | None:
    operating = operating_cash_flow.get("val") if operating_cash_flow else None
    capex = capital_expenditures.get("val") if capital_expenditures else None
    if operating is None:
        return None
    if capex is None:
        return float(operating)
    capex_value = float(capex)
    return float(operating) + capex_value if capex_value < 0 else float(operating) - capex_value
