"""SEC EDGAR helpers."""

from __future__ import annotations

import json
from email.utils import parsedate_to_datetime
import threading
import time
from typing import Any
from urllib.parse import quote, urlparse

import httpx


RETRYABLE_OFFICIAL_STATUSES = frozenset({403, 429, 500, 502, 503, 504})
OFFICIAL_HOST_MIN_INTERVAL_SECONDS = 0.2
OFFICIAL_MAX_RETRY_WAIT_SECONDS = 30.0
_host_lock = threading.Lock()
_host_last_request: dict[str, float] = {}


class OfficialSourceHTTPError(RuntimeError):
    """Concise terminal error for an official-source request."""

    def __init__(self, provider: str, url: str, status_code: int, attempts: int, detail: str = "") -> None:
        host = urlparse(url).netloc or url
        suffix = f": {detail[:240]}" if detail else ""
        super().__init__(f"{provider} {host} HTTP {status_code} after {attempts} attempt(s){suffix}")
        self.provider = provider
        self.url = url
        self.status_code = status_code
        self.attempts = attempts


def _retry_after_seconds(value: str | None, *, now: float | None = None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, min(OFFICIAL_MAX_RETRY_WAIT_SECONDS, float(value.strip())))
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        return None
    reference = time.time() if now is None else now
    return max(0.0, min(OFFICIAL_MAX_RETRY_WAIT_SECONDS, retry_at.timestamp() - reference))


def _wait_for_host(host: str, *, min_interval_seconds: float) -> None:
    if min_interval_seconds <= 0:
        return
    with _host_lock:
        current = time.monotonic()
        previous = _host_last_request.get(host)
        wait = min_interval_seconds - (current - previous) if previous is not None else 0.0
        if wait > 0:
            time.sleep(wait)
        _host_last_request[host] = time.monotonic()


def official_get_bytes(
    url: str,
    user_agent: str,
    *,
    provider: str = "official source",
    timeout_seconds: float = 20.0,
    max_retries: int = 2,
    min_interval_seconds: float = OFFICIAL_HOST_MIN_INTERVAL_SECONDS,
) -> bytes:
    """Fetch an official endpoint with one bounded SEC/BLS request policy."""

    parsed = urlparse(url)
    host = parsed.netloc
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Host": host,
    }
    retries = max(0, int(max_retries))
    for attempt in range(retries + 1):
        _wait_for_host(host, min_interval_seconds=min_interval_seconds)
        response = httpx.get(url, headers=headers, timeout=timeout_seconds, follow_redirects=True)
        if 200 <= response.status_code < 300:
            return response.content
        if response.status_code not in RETRYABLE_OFFICIAL_STATUSES or attempt >= retries:
            detail = " ".join(str(getattr(response, "text", "") or "").split())
            raise OfficialSourceHTTPError(provider, url, response.status_code, attempt + 1, detail)
        retry_after = _retry_after_seconds(
            response.headers.get("Retry-After") if getattr(response, "headers", None) is not None else None
        )
        wait = retry_after if retry_after is not None else min(
            OFFICIAL_MAX_RETRY_WAIT_SECONDS,
            0.25 * (2**attempt),
        )
        time.sleep(wait)
    raise AssertionError("official source retry loop exhausted")


def sec_get_bytes(
    url: str,
    user_agent: str,
    *,
    timeout_seconds: float = 20.0,
    max_retries: int = 2,
) -> bytes:
    return official_get_bytes(
        url,
        user_agent,
        provider="SEC",
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )


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
