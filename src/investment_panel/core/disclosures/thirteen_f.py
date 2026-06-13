"""Auto-split from core/disclosures.py — see ARCHITECTURE.md."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import xml.etree.ElementTree as ET
from typing import Any
from investment_panel.core import sec
from investment_panel.core.db import json_dumps

from investment_panel.core.disclosures.config import normalize_cusip
from investment_panel.core.disclosures.constants import THIRTEEN_F_CAVEAT, THIRTEEN_F_FORMS, stable_id


def purge_direct_tracker_rows(con: Any) -> None:
    con.execute("DELETE FROM disclosures WHERE source_type = 'pelositracker_portfolio'")


def recent_13f_filings(submissions: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    recent = (submissions.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    filings: list[dict[str, Any]] = []
    for index, form in enumerate(forms):
        if form not in THIRTEEN_F_FORMS:
            continue
        accession_number = _recent_value(recent, "accessionNumber", index)
        if not accession_number:
            continue
        filings.append(
            {
                "accession_number": accession_number,
                "form": form,
                "filed_date": _recent_value(recent, "filingDate", index),
                "report_date": _recent_value(recent, "reportDate", index),
                "acceptance_datetime": _recent_value(recent, "acceptanceDateTime", index),
                "primary_document": _recent_value(recent, "primaryDocument", index),
                "primary_doc_description": _recent_value(recent, "primaryDocDescription", index),
                "filer_name": submissions.get("name"),
                "cik": str(submissions.get("cik") or "").zfill(10),
            }
        )
        if len(filings) >= limit:
            break
    return filings


def ingest_13f_trackers(
    con: Any,
    trackers: list[dict[str, Any]],
    user_agent: str,
    default_max_filings: int = 3,
    fetch_holdings: bool = True,
) -> dict[str, int]:
    filings_seen = 0
    filings_ingested = 0
    holdings_ingested = 0
    trackers_checked = 0
    for tracker in trackers:
        cik = str(tracker["cik"])
        trackers_checked += 1
        submissions = sec.company_submissions(cik, user_agent)
        max_filings = int(tracker.get("max_filings") or default_max_filings)
        for filing in recent_13f_filings(submissions, max_filings):
            filings_seen += 1
            holding_payload = {"holdings": [], "source_url": None, "status": "not_requested", "error": None}
            if fetch_holdings:
                holding_payload = fetch_13f_holding_payload(cik, filing["accession_number"], filing.get("primary_document"), user_agent)
            else:
                disclosure_id = stable_id(f"13f:{str(cik).zfill(10)}:{filing['accession_number']}")
                holding_payload = preserve_existing_13f_holdings_when_not_requested(con, disclosure_id, holding_payload)
            holdings = holding_payload["holdings"]
            holdings_ingested += len(holdings)
            upsert_13f_disclosure(con, tracker, filing, holding_payload)
            filings_ingested += 1
    return {
        "trackers_checked": trackers_checked,
        "filings_seen": filings_seen,
        "filings_ingested": filings_ingested,
        "holdings_ingested": holdings_ingested,
    }


def upsert_13f_disclosure(
    con: Any,
    tracker: dict[str, Any],
    filing: dict[str, Any],
    holding_payload: dict[str, Any],
) -> None:
    cik = str(tracker["cik"])
    accession_number = filing["accession_number"]
    disclosure_id = stable_id(f"13f:{str(cik).zfill(10)}:{accession_number}")
    holding_payload = preserve_existing_13f_holdings_when_not_requested(con, disclosure_id, holding_payload)
    holdings = resolve_13f_holding_tickers(holding_payload.get("holdings") or [], tracker.get("ticker_map") or {})
    total_value = sum(int(row.get("value_thousands") or 0) for row in holdings)
    source_url = holding_payload.get("source_url") or sec.filing_index_url(cik, accession_number)
    mapped_count = sum(1 for row in holdings if row.get("symbol"))
    raw = {
        "source_type": "13f",
        "tracker_name": tracker.get("name"),
        "cik": str(cik).zfill(10),
        "filer_name": filing.get("filer_name") or tracker.get("name"),
        "form": filing.get("form"),
        "accession_number": accession_number,
        "report_date": filing.get("report_date"),
        "filed_date": filing.get("filed_date"),
        "acceptance_datetime": filing.get("acceptance_datetime"),
        "primary_document": filing.get("primary_document"),
        "primary_doc_description": filing.get("primary_doc_description"),
        "lag_caveat": THIRTEEN_F_CAVEAT,
        "as_of_caveat": "Holdings are as of the filing report date, not the ingestion date.",
        "ticker_mapping_status": "mapped" if mapped_count == len(holdings) and holdings else "partial" if mapped_count else "unmapped",
        "ticker_mapping_count": mapped_count,
        "ticker_unmapped_count": len(holdings) - mapped_count,
        "holdings_parse_status": holding_payload.get("status"),
        "holdings_parse_error": holding_payload.get("error"),
        "holdings_source_url": holding_payload.get("source_url"),
        "holdings_count": len(holdings),
        "holdings_value_thousands": total_value if holdings else None,
        "next_filing_due_date": next_13f_filing_due_date(filing.get("report_date")),
        "holdings": holdings,
    }
    con.execute(
        """
        INSERT OR REPLACE INTO disclosures
        (id, source_type, trader_name, filer_name, symbol, event_date, filed_date, action, amount, raw, source_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            disclosure_id,
            "13f",
            tracker.get("name"),
            filing.get("filer_name") or tracker.get("name"),
            None,
            filing.get("report_date"),
            filing.get("filed_date"),
            filing.get("form"),
            str(total_value) if holdings else None,
            json_dumps(raw),
            source_url,
        ],
    )


def preserve_existing_13f_holdings_when_not_requested(con: Any, disclosure_id: str, holding_payload: dict[str, Any]) -> dict[str, Any]:
    if holding_payload.get("status") != "not_requested":
        return holding_payload
    row = con.execute("SELECT raw FROM disclosures WHERE id = ?", [disclosure_id]).fetchone()
    if not row:
        return holding_payload
    raw_value = row[0]
    if isinstance(raw_value, str):
        try:
            raw = json.loads(raw_value)
        except json.JSONDecodeError:
            return holding_payload
    elif isinstance(raw_value, dict):
        raw = raw_value
    else:
        return holding_payload
    holdings = raw.get("holdings")
    if not isinstance(holdings, list) or not holdings:
        return holding_payload
    return {
        "holdings": holdings,
        "source_url": raw.get("holdings_source_url"),
        "status": raw.get("holdings_parse_status") or "parsed",
        "error": raw.get("holdings_parse_error"),
    }


def resolve_13f_holding_tickers(holdings: list[dict[str, Any]], ticker_map: dict[str, str]) -> list[dict[str, Any]]:
    resolved = []
    for holding in holdings:
        row = dict(holding)
        cusip = normalize_cusip(row.get("cusip"))
        symbol = ticker_map.get(cusip)
        if symbol:
            row["symbol"] = symbol
            row["ticker_mapping_source"] = "configured_cusip_map"
        else:
            row["symbol"] = None
            row["ticker_mapping_source"] = "unresolved"
        resolved.append(row)
    return resolved


def next_13f_filing_due_date(report_date: Any) -> str | None:
    try:
        current = datetime.strptime(str(report_date)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    month = current.month + 3
    year = current.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    next_quarter_end = quarter_end_date(year, month)
    return (next_quarter_end + timedelta(days=45)).isoformat()


def quarter_end_date(year: int, month: int) -> date:
    quarter_end_month = ((month - 1) // 3 + 1) * 3
    if quarter_end_month == 12:
        return date(year, 12, 31)
    return date(year, quarter_end_month + 1, 1) - timedelta(days=1)


def fetch_13f_holding_payload(
    cik: str,
    accession_number: str,
    primary_document: str | None,
    user_agent: str,
) -> dict[str, Any]:
    try:
        index = sec.filing_directory_index(cik, accession_number, user_agent)
        candidates = information_table_candidates(index, primary_document)
        for filename in candidates:
            xml_text = sec.filing_document_text(cik, accession_number, filename, user_agent)
            holdings = parse_information_table_xml(xml_text)
            if holdings:
                return {
                    "holdings": holdings,
                    "source_url": sec.filing_document_url(cik, accession_number, filename),
                    "status": "parsed",
                    "error": None,
                }
        return fetch_13f_holdings_from_submission_text(cik, accession_number, user_agent)
    except Exception as exc:
        fallback = fetch_13f_holdings_from_submission_text(cik, accession_number, user_agent)
        if fallback["holdings"]:
            fallback["status"] = "parsed_from_submission_text_after_archive_error"
            fallback["error"] = str(exc)
            return fallback
        return {"holdings": [], "source_url": None, "status": "error", "error": str(exc)}


def fetch_13f_holdings_from_submission_text(cik: str, accession_number: str, user_agent: str) -> dict[str, Any]:
    try:
        text = sec.complete_submission_text(cik, accession_number, user_agent)
        for document in split_sec_documents(text):
            doc_type = document.get("type", "").lower()
            filename = document.get("filename")
            body = document.get("body", "")
            if "information table" in doc_type or "infotable" in (filename or "").lower() or "13f" in (filename or "").lower():
                holdings = parse_information_table_xml(body)
                if holdings:
                    return {
                        "holdings": holdings,
                        "source_url": sec.filing_document_url(cik, accession_number, f"{accession_number}.txt"),
                        "status": "parsed_from_submission_text",
                        "error": None,
                    }
        return {"holdings": [], "source_url": None, "status": "not_found", "error": None}
    except Exception as exc:
        return {"holdings": [], "source_url": None, "status": "submission_text_error", "error": str(exc)}


def information_table_candidates(index_json: dict[str, Any], primary_document: str | None = None) -> list[str]:
    items = ((index_json.get("directory") or {}).get("item") or [])
    primary = (primary_document or "").lower()
    xml_names = [
        str(item.get("name"))
        for item in items
        if isinstance(item, dict) and str(item.get("name", "")).lower().endswith(".xml")
    ]
    filtered = [name for name in xml_names if name.lower() not in {primary, "filingsummary.xml"}]
    return sorted(filtered, key=_information_table_rank)


def parse_information_table_xml(xml_text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text.encode("utf-8"))
    holdings: list[dict[str, Any]] = []
    for info_table in root.iter():
        if _local_name(info_table.tag) != "infoTable":
            continue
        row = _direct_text(info_table)
        shares = _first_child_text(info_table, "shrsOrPrnAmt")
        voting = _first_child_text(info_table, "votingAuthority")
        holdings.append(
            {
                "name": row.get("nameOfIssuer"),
                "title": row.get("titleOfClass"),
                "cusip": row.get("cusip"),
                "value_thousands": _int_or_none(row.get("value")),
                "shares_or_principal_amount": _int_or_none(shares.get("sshPrnamt")),
                "shares_or_principal_type": shares.get("sshPrnamtType"),
                "put_call": row.get("putCall"),
                "investment_discretion": row.get("investmentDiscretion"),
                "other_manager": row.get("otherManager"),
                "voting_authority": {
                    "sole": _int_or_none(voting.get("Sole")),
                    "shared": _int_or_none(voting.get("Shared")),
                    "none": _int_or_none(voting.get("None")),
                },
            }
        )
    return holdings


def split_sec_documents(submission_text: str) -> list[dict[str, str]]:
    documents: list[dict[str, str]] = []
    for chunk in submission_text.split("<DOCUMENT>")[1:]:
        body = chunk.split("</DOCUMENT>", 1)[0]
        documents.append(
            {
                "type": _tag_text(body, "TYPE"),
                "filename": _tag_text(body, "FILENAME"),
                "description": _tag_text(body, "DESCRIPTION"),
                "body": _body_text(body),
            }
        )
    return documents


def _recent_value(recent: dict[str, Any], key: str, index: int) -> Any:
    values = recent.get(key) or []
    if index >= len(values):
        return None
    return values[index]


def _tag_text(text: str, tag: str) -> str:
    marker = f"<{tag}>"
    if marker not in text:
        return ""
    return text.split(marker, 1)[1].splitlines()[0].strip()


def _body_text(text: str) -> str:
    if "<XML>" in text and "</XML>" in text:
        return text.split("<XML>", 1)[1].split("</XML>", 1)[0].strip()
    if "<TEXT>" in text and "</TEXT>" in text:
        return text.split("<TEXT>", 1)[1].split("</TEXT>", 1)[0].strip()
    return text


def _information_table_rank(filename: str) -> tuple[int, str]:
    lower = filename.lower()
    if "infotable" in lower or "info_table" in lower:
        return (0, lower)
    if "form13f" in lower or "13f" in lower:
        return (1, lower)
    if "primary" in lower:
        return (3, lower)
    return (2, lower)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _direct_text(element: ET.Element) -> dict[str, str]:
    values: dict[str, str] = {}
    for child in list(element):
        if list(child):
            continue
        if child.text and child.text.strip():
            values[_local_name(child.tag)] = child.text.strip()
    return values


def _first_child_text(element: ET.Element, child_name: str) -> dict[str, str]:
    for child in list(element):
        if _local_name(child.tag) == child_name:
            return _direct_text(child)
    return {}


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).replace(",", ""))
    except ValueError:
        return None
