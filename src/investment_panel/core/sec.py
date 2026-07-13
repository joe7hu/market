"""SEC EDGAR helpers."""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import quote, urlparse

import httpx


def sec_get_bytes(
    url: str,
    user_agent: str,
    *,
    timeout_seconds: float = 20.0,
    max_retries: int = 2,
) -> bytes:
    host = urlparse(url).netloc
    headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
        "Host": host,
    }
    for attempt in range(max_retries + 1):
        response = httpx.get(url, headers=headers, timeout=timeout_seconds, follow_redirects=True)
        if response.status_code not in {403, 429, 503} or attempt >= max_retries:
            response.raise_for_status()
            return response.content
        time.sleep(0.25 * (2**attempt))
    raise AssertionError("unreachable")


def sec_get_json(url: str, user_agent: str) -> dict[str, Any]:
    return json.loads(sec_get_bytes(url, user_agent))


def sec_get_text(url: str, user_agent: str) -> str:
    return sec_get_bytes(url, user_agent).decode("utf-8", errors="replace")


def company_submissions(cik: str, user_agent: str) -> dict[str, Any]:
    padded = str(cik).zfill(10)
    return sec_get_json(f"https://data.sec.gov/submissions/CIK{padded}.json", user_agent)


def company_facts(cik: str, user_agent: str) -> dict[str, Any]:
    padded = str(cik).zfill(10)
    return sec_get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{padded}.json", user_agent)


def company_tickers(user_agent: str) -> dict[str, Any]:
    return sec_get_json("https://www.sec.gov/files/company_tickers.json", user_agent)


def archive_accession_path(cik: str, accession_number: str) -> str:
    cik_path = str(int(str(cik).strip().lstrip("0") or "0"))
    accession_path = str(accession_number).replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_path}/{accession_path}"


def filing_index_url(cik: str, accession_number: str) -> str:
    return f"{archive_accession_path(cik, accession_number)}/index.json"


def filing_document_url(cik: str, accession_number: str, filename: str) -> str:
    safe_name = quote(filename.strip().lstrip("/"))
    return f"{archive_accession_path(cik, accession_number)}/{safe_name}"


def filing_directory_index(cik: str, accession_number: str, user_agent: str) -> dict[str, Any]:
    return sec_get_json(filing_index_url(cik, accession_number), user_agent)


def filing_document_text(cik: str, accession_number: str, filename: str, user_agent: str) -> str:
    return sec_get_text(filing_document_url(cik, accession_number, filename), user_agent)


def complete_submission_text(cik: str, accession_number: str, user_agent: str) -> str:
    filename = f"{accession_number}.txt"
    return sec_get_text(filing_document_url(cik, accession_number, filename), user_agent)
