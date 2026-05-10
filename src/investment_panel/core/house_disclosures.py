"""Official House financial disclosure search and PDF parsing."""

from __future__ import annotations

import io
import re
from typing import Any
from urllib.parse import urljoin

import httpx
from pypdf import PdfReader


HOUSE_SEARCH_URL = "https://disclosures-clerk.house.gov/FinancialDisclosure/ViewMemberSearchResult"
HOUSE_BASE_URL = "https://disclosures-clerk.house.gov/"
ASSET_RE = re.compile(r"\((?P<symbol>[A-Z][A-Z0-9.]{0,9})\)\s*\[(?P<asset_type>[A-Z]{2})\]")
DATE_PAIR_RE = re.compile(r"(?P<date>\d{2}/\d{2}/\d{4})(?P<notification>\d{2}/\d{2}/\d{4})")
AMOUNT_RE = re.compile(r"\$[\d,]+(?:\.\d+)?(?:\s*-\s*\$[\d,]+(?:\.\d+)?)?|Over\s+\$[\d,]+|None", re.I)
ANNUAL_HOLDING_RE = re.compile(
    r"^\s*(?P<owner>SP|JT|DC|dependent child)\s+(?P<amount>\$[\d,]+(?:\.\d+)?(?:\s*-\s*\$[\d,]+(?:\.\d+)?)?|Over\s+\$[\d,]+|None)\b",
    re.I | re.S,
)
SHARES_RE = re.compile(r"(?P<shares>[\d,]+)\s+shares", re.I)
OPTIONS_RE = re.compile(r"(?P<contracts>[\d,]+)\s+call options", re.I)


def search_house_member_filings(
    last_name: str,
    start_year: int,
    end_year: int,
    user_agent: str,
    state: str | None = None,
    district: str | None = None,
) -> list[dict[str, Any]]:
    headers = {"User-Agent": user_agent}
    filings: list[dict[str, Any]] = []
    with httpx.Client(timeout=20.0, headers=headers, follow_redirects=True) as client:
        for year in range(start_year, end_year + 1):
            response = client.post(
                HOUSE_SEARCH_URL,
                data={
                    "LastName": last_name,
                    "FilingYear": str(year),
                    "State": state or "",
                    "District": district or "",
                },
            )
            response.raise_for_status()
            filings.extend(parse_house_search_results(response.text, year))
    return filings


def parse_house_search_results(html: str, year: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pattern = re.compile(
        r'<a href="(?P<href>[^"]+)"[^>]*>(?P<name>.*?)</a>.*?'
        r'<td data-label="Office">(?P<office>.*?)</td>.*?'
        r'<td data-label="Filing Year">(?P<year>.*?)</td>.*?'
        r'<td data-label="Filing">(?P<filing>.*?)</td>',
        re.S,
    )
    for match in pattern.finditer(html):
        href = match.group("href")
        filing_type = _clean(match.group("filing"))
        rows.append(
            {
                "name": _clean(match.group("name")),
                "office": _clean(match.group("office")),
                "filing_year": int(_clean(match.group("year")) or year),
                "filing_type": filing_type,
                "url": urljoin(HOUSE_BASE_URL, href),
                "document_id": href.rsplit("/", 1)[-1].removesuffix(".pdf"),
            }
        )
    return rows


def fetch_house_pdf_text(url: str, user_agent: str) -> str:
    with httpx.Client(timeout=30.0, headers={"User-Agent": user_agent}, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
    reader = PdfReader(io.BytesIO(response.content))
    return clean_pdf_text("\n".join(page.extract_text() or "" for page in reader.pages))


def clean_pdf_text(text: str) -> str:
    return text.replace("\x00", "").replace("\ufeff", "")


def parse_house_disclosure_text(text: str, filing: dict[str, Any], trader_name: str) -> list[dict[str, Any]]:
    if "Periodic Transaction Report" in text or "P T R" in text:
        return parse_house_ptr_text(text, filing, trader_name)
    if "Financial Disclosure Report" in text or "F D R" in text:
        return parse_house_annual_text(text, filing, trader_name)
    return []


def parse_house_ptr_text(text: str, filing: dict[str, Any], trader_name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    chunks = re.split(r"\n(?=SP\s+)", text)
    for chunk in chunks:
        asset_match = ASSET_RE.search(chunk)
        date_match = DATE_PAIR_RE.search(chunk)
        if not asset_match or not date_match:
            continue
        before_date = chunk[: date_match.start()]
        action_match = re.search(r"(S\s+\(partial\)|S|P|E)\s*$", before_date.strip())
        if not action_match:
            continue
        amount_match = AMOUNT_RE.search(chunk[date_match.end() :])
        comment = comment_text(chunk)
        rows.append(
            {
                "id": f"house:{filing['document_id']}:{len(rows)}",
                "trader_name": trader_name,
                "filer_name": filing.get("name") or trader_name,
                "symbol": asset_match.group("symbol"),
                "asset_name": clean_asset_name(before_date[: action_match.start()]),
                "asset_type": asset_match.group("asset_type"),
                "transaction_type": normalize_house_action(action_match.group(1)),
                "transaction_date": _iso_date(date_match.group("date")),
                "filed_date": filing_signed_date(text) or _iso_date(date_match.group("notification")),
                "amount": amount_match.group(0) if amount_match else "",
                "source_url": filing["url"],
                "source_document_id": filing["document_id"],
                "comment": comment,
                "shares": shares_from_comment(comment),
                "contracts": contracts_from_comment(comment),
            }
        )
    return rows


def parse_house_annual_text(text: str, filing: dict[str, Any], trader_name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    position_date = f"{filing.get('filing_year')}-12-31"
    filed_date = annual_filing_date(text) or position_date
    for match in ASSET_RE.finditer(text):
        asset_type = match.group("asset_type")
        if asset_type not in {"ST", "OP"}:
            continue
        holding_match = ANNUAL_HOLDING_RE.match(text[match.end() : match.end() + 120])
        if not holding_match or holding_match.group("amount").lower() == "none":
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        comment = following_comment(text, match.end())
        rows.append(
            {
                "id": f"house:{filing['document_id']}:annual:{len(rows)}",
                "trader_name": trader_name,
                "filer_name": filing.get("name") or trader_name,
                "symbol": match.group("symbol"),
                "asset_name": clean_asset_name(text[line_start : match.start()]),
                "asset_type": asset_type,
                "transaction_type": "BASELINE",
                "transaction_date": position_date,
                "filed_date": filed_date,
                "amount": holding_match.group("amount"),
                "source_url": filing["url"],
                "source_document_id": filing["document_id"],
                "comment": comment,
                "shares": shares_from_comment(comment),
                "contracts": contracts_from_comment(comment),
            }
        )
    return rows


def normalize_house_action(value: str) -> str:
    normalized = value.upper()
    if normalized.startswith("S"):
        return "SELL"
    if normalized.startswith("E"):
        return "EXCHANGE"
    return "BUY"


def _iso_date(value: str) -> str:
    month, day, year = value.split("/")
    return f"{year}-{month}-{day}"


def _clean(value: str) -> str:
    return re.sub(r"<.*?>", "", value).replace("&nbsp;", " ").strip()


def clean_asset_name(value: str) -> str:
    value = re.sub(r"^SP\s+", "", value.strip())
    value = re.sub(r"Filing ID #\d+", "", value)
    value = re.sub(r"Asset Owner Value.*", "", value, flags=re.S)
    return " ".join(value.split())


def comment_text(chunk: str) -> str:
    marker = "D:"
    if marker not in chunk:
        return ""
    comment = chunk.split(marker, 1)[1]
    return " ".join(comment.splitlines()).strip()


def following_comment(text: str, start: int) -> str:
    window = text[start : start + 500]
    if "D:" not in window:
        return ""
    return " ".join(window.split("D:", 1)[1].splitlines()[:2]).strip()


def shares_from_comment(comment: str) -> float | None:
    match = SHARES_RE.search(comment or "")
    return float(match.group("shares").replace(",", "")) if match else None


def contracts_from_comment(comment: str) -> float | None:
    match = OPTIONS_RE.search(comment or "")
    return float(match.group("contracts").replace(",", "")) if match else None


def filing_signed_date(text: str) -> str | None:
    match = re.search(r"Digitally Signed:.*?,\s*(\d{2}/\d{2}/\d{4})", text)
    return _iso_date(match.group(1)) if match else None


def annual_filing_date(text: str) -> str | None:
    match = re.search(r"Filing Date:\s*(\d{2}/\d{2}/\d{4})", text)
    return _iso_date(match.group(1)) if match else None
