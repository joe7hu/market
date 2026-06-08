"""SEC EDGAR helpers."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx


def sec_get_json(url: str, user_agent: str) -> dict[str, Any]:
    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
    with httpx.Client(timeout=20.0, headers=headers) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.json()


def sec_get_text(url: str, user_agent: str) -> str:
    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
    with httpx.Client(timeout=20.0, headers=headers) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text


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
