"""Broad market valuation and environment snapshots for the Market page."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any

import httpx

from investment_panel.core.db import json_dumps


MUNGER_MARKET_METRICS_URL = "https://mungermode.com/api/v1/market/metrics"
FRED_TEN_YEAR_YIELD_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"
HISTORY_OF_MARKET_FORWARD_PE_URL = "https://historyofmarket.com/api/sp500/forward-pe.json"
FULLSTACK_MARKET_MODEL_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vSaW8H7OeWjfAO2Gcv69Qy541T7BdwULlob4gdKL8LMb97qAmHCn1sMTIzJPgVsm4hmQVBK7NEoEQab/"
    "pub?gid=1536241650&single=true&output=csv"
)

VALUATION_METRICS = {"sp500_forward_pe", "shiller_pe", "sp500_pe", "equity_risk_premium", "sp500_price"}
ENVIRONMENT_GROUPS = {"Market", "Sectors", "Industries", "Managed ETFs", "Countries", "Others", "Macro"}
MULTPL_VALUATION_METRICS = {
    "shiller_pe": ("Shiller P/E (CAPE)", "https://www.multpl.com/shiller-pe/table/by-month", "x", False),
    "sp500_pe": ("S&P 500 Trailing P/E", "https://www.multpl.com/s-p-500-pe-ratio/table/by-month", "x", False),
    "sp500_price": ("S&P 500 Price", "https://www.multpl.com/s-p-500-historical-prices/table/by-month", "", True),
}


def store_market_environment_sources(con: Any) -> int:
    """Persist public broad-market model inputs used by /market."""

    count = 0
    for loader in (
        store_munger_market_metrics,
        store_multpl_valuation_metrics,
        store_history_of_market_forward_pe_metric,
        store_equity_risk_premium_metric,
        store_fullstack_market_model,
    ):
        try:
            count += loader(con)
        except (httpx.HTTPError, ValueError):
            continue
    return count


def store_munger_market_metrics(con: Any, url: str = MUNGER_MARKET_METRICS_URL) -> int:
    response = httpx.get(url, timeout=30.0, follow_redirects=True, headers={"User-Agent": "joehu-market-panel/0.1"})
    response.raise_for_status()
    payload = response.json()
    count = 0
    for metric, block in payload.items():
        if metric not in VALUATION_METRICS or not isinstance(block, dict):
            continue
        label = str(block.get("label") or metric)
        suffix = str(block.get("suffix") or "")
        higher_is_better = bool(block.get("higher_is_better"))
        source = str(block.get("source") or "mungermode_market_metrics")
        for point in block.get("data") or []:
            if not isinstance(point, dict):
                continue
            as_of = _date_string(point.get("date"))
            value = _number(point.get("value"))
            if not as_of or value is None:
                continue
            con.execute(
                """
                INSERT OR REPLACE INTO market_valuation_metric_points
                (metric, as_of, label, value, suffix, higher_is_better, source, source_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [metric, as_of, label, value, suffix, higher_is_better, source, url],
            )
            count += 1
    return count


def store_multpl_valuation_metrics(con: Any) -> int:
    count = 0
    for metric, (label, url, suffix, higher_is_better) in MULTPL_VALUATION_METRICS.items():
        response = httpx.get(url, timeout=30.0, follow_redirects=True, headers={"User-Agent": "joehu-market-panel/0.1"})
        response.raise_for_status()
        for as_of, value in parse_multpl_valuation_table(response.text):
            con.execute(
                """
                INSERT OR REPLACE INTO market_valuation_metric_points
                (metric, as_of, label, value, suffix, higher_is_better, source, source_url)
                VALUES (?, ?, ?, ?, ?, ?, 'multpl', ?)
                """,
                [metric, as_of, label, value, suffix, higher_is_better, url],
            )
            count += 1
    return count


def store_history_of_market_forward_pe_metric(con: Any, url: str = HISTORY_OF_MARKET_FORWARD_PE_URL) -> int:
    response = httpx.get(url, timeout=30.0, follow_redirects=True, headers={"User-Agent": "joehu-market-panel/0.1"})
    response.raise_for_status()
    count = 0
    for as_of, value in parse_history_of_market_forward_pe_json(response.json()):
        con.execute(
            """
            INSERT OR REPLACE INTO market_valuation_metric_points
            (metric, as_of, label, value, suffix, higher_is_better, source, source_url)
            VALUES ('sp500_forward_pe', ?, 'S&P 500 Forward P/E', ?, 'x', false, 'historyofmarket', ?)
            """,
            [as_of, value, url],
        )
        count += 1
    return count


def store_equity_risk_premium_metric(
    con: Any,
    ten_year_url: str = FRED_TEN_YEAR_YIELD_CSV_URL,
) -> int:
    cape_rows = con.execute(
        """
        SELECT as_of, value
        FROM market_valuation_metric_points
        WHERE metric = 'shiller_pe'
        ORDER BY as_of
        """
    ).fetchall()
    if not cape_rows:
        return 0
    response = httpx.get(
        ten_year_url,
        timeout=httpx.Timeout(90.0, connect=15.0),
        follow_redirects=True,
        headers={"User-Agent": "python-httpx", "Accept": "text/csv"},
    )
    response.raise_for_status()
    yields_by_month = parse_fred_ten_year_yield_csv(response.text)
    count = 0
    for as_of, cape in cape_rows:
        cape_value = _number(cape)
        as_of_text = _date_string(as_of)
        if not cape_value or not as_of_text:
            continue
        ten_year_yield = yields_by_month.get(as_of_text[:7])
        if ten_year_yield is None:
            continue
        erp = (100 / cape_value) - ten_year_yield
        con.execute(
            """
            INSERT OR REPLACE INTO market_valuation_metric_points
            (metric, as_of, label, value, suffix, higher_is_better, source, source_url)
            VALUES ('equity_risk_premium', ?, 'Equity Risk Premium (1/CAPE - 10Y)', ?, '%', true, 'multpl_fred', ?)
            """,
            [as_of_text, round(erp, 4), ten_year_url],
        )
        count += 1
    return count


def store_fullstack_market_model(con: Any, url: str = FULLSTACK_MARKET_MODEL_CSV_URL) -> int:
    response = httpx.get(url, timeout=30.0, follow_redirects=True, headers={"User-Agent": "joehu-market-panel/0.1"})
    response.raise_for_status()
    text = response.text
    records = parse_fullstack_market_model_csv(text, source_url=url)
    count = 0
    for record in records:
        con.execute(
            """
            INSERT OR REPLACE INTO market_environment_asset_snapshots
            (symbol, as_of, group_name, name, price, return_1d, return_ytd, return_1w,
             return_1m, return_1y, pct_from_52w_high, sma_10_up, sma_20_up, sma_50_up,
             sma_200_up, sma_20_gt_50, sma_50_gt_200, range_ratio_52w, color, source, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                record["symbol"],
                record["as_of"],
                record["group_name"],
                record.get("name"),
                record.get("price"),
                record.get("return_1d"),
                record.get("return_ytd"),
                record.get("return_1w"),
                record.get("return_1m"),
                record.get("return_1y"),
                record.get("pct_from_52w_high"),
                record.get("sma_10_up"),
                record.get("sma_20_up"),
                record.get("sma_50_up"),
                record.get("sma_200_up"),
                record.get("sma_20_gt_50"),
                record.get("sma_50_gt_200"),
                record.get("range_ratio_52w"),
                record.get("color"),
                "market_environment_asset_matrix",
                json_dumps(record.get("raw") or {}),
            ],
        )
        count += 1
    return count


def parse_multpl_valuation_table(text: str) -> list[tuple[str, float]]:
    records: list[tuple[str, float]] = []
    for row in _HTMLTableRows(text).rows:
        if len(row) < 2 or row[0].lower() == "date":
            continue
        as_of = _date_from_text(row[0])
        value = _number(row[1].replace("†", ""))
        if as_of and value is not None:
            records.append((as_of, value))
    return records


def parse_history_of_market_forward_pe_json(payload: dict[str, Any]) -> list[tuple[str, float]]:
    records: list[tuple[str, float]] = []
    for point in payload.get("forward") or []:
        if not isinstance(point, dict):
            continue
        as_of = _date_from_text(point.get("date"))
        value = _number(point.get("value"))
        if as_of and value is not None:
            records.append((as_of, value))
    return records


def parse_fred_ten_year_yield_csv(text: str) -> dict[str, float]:
    by_month: dict[str, float] = {}
    for row in csv.DictReader(io.StringIO(text)):
        date_value = row.get("observation_date") or row.get("DATE") or row.get("date")
        yield_value = _number(row.get("DGS10"))
        as_of = _date_from_text(date_value)
        if as_of and yield_value is not None:
            by_month[as_of[:7]] = yield_value
    return by_month


def parse_fullstack_market_model_csv(text: str, source_url: str = FULLSTACK_MARKET_MODEL_CSV_URL) -> list[dict[str, Any]]:
    rows = list(csv.reader(io.StringIO(text)))
    as_of = _published_date(rows) or date.today().isoformat()
    records: list[dict[str, Any]] = []
    group_name: str | None = None
    for row in rows:
        padded = [*row, *[""] * 40]
        label = padded[1].strip()
        if label in ENVIRONMENT_GROUPS:
            group_name = label
            continue
        if not group_name or not label or label == "Ticker":
            continue
        if not padded[2].strip() or padded[2].strip() == "Index":
            continue
        price = _number(padded[3])
        if price is None:
            continue
        records.append(
            {
                "symbol": label.upper(),
                "as_of": as_of,
                "group_name": group_name,
                "name": padded[2].strip(),
                "price": price,
                "return_1d": _percent(padded[4]),
                "return_ytd": _percent(padded[6]),
                "return_1w": _percent(padded[7]),
                "return_1m": _percent(padded[8]),
                "return_1y": _percent(padded[9]),
                "pct_from_52w_high": _percent(padded[13]),
                "sma_10_up": _arrow_up(padded[15]),
                "sma_20_up": _arrow_up(padded[16]),
                "sma_50_up": _arrow_up(padded[17]),
                "sma_200_up": _arrow_up(padded[18]),
                "sma_20_gt_50": _arrow_up(padded[19]),
                "sma_50_gt_200": _arrow_up(padded[20]),
                "range_ratio_52w": _percent(padded[31]),
                "color": padded[32].strip() or None,
                "source_url": source_url,
                "raw": {"row": row},
            }
        )
    return records


def _published_date(rows: list[list[str]]) -> str | None:
    for row in reversed(rows):
        if len(row) < 2:
            continue
        value = row[1].strip()
        if not value:
            continue
        try:
            return datetime.strptime(value, "%B %d, %Y").date().isoformat()
        except ValueError:
            continue
    return None


def _date_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:10]


def _date_from_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%B %Y", "%b %Y"):
        try:
            return datetime.strptime(text[:32], fmt).date().isoformat()
        except ValueError:
            continue
    return _date_string(text)


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace(",", "").replace("$", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _percent(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    return _number(text.replace("%", ""))


def _arrow_up(value: Any) -> bool | None:
    text = str(value or "").strip()
    normalized = text.lower()
    if text == "▲" or normalized in {"up", "true", "1", "yes"}:
        return True
    if text == "▼" or normalized in {"down", "false", "0", "no"}:
        return False
    return None


class _HTMLTableRows(HTMLParser):
    def __init__(self, text: str):
        super().__init__()
        self.rows: list[list[str]] = []
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None
        self.feed(text)

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._current_row = []
        elif tag in {"td", "th"} and self._current_row is not None:
            self._current_cell = []

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._current_cell is not None and self._current_row is not None:
            value = unescape(" ".join(self._current_cell)).replace("\u2002", " ")
            self._current_row.append(" ".join(value.split()))
            self._current_cell = None
        elif tag == "tr" and self._current_row is not None:
            if self._current_row:
                self.rows.append(self._current_row)
            self._current_row = None
