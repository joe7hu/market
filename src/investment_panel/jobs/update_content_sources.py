"""Fetch configured news, blogs, and X sources into compact PostgreSQL facts."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import gzip
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import httpx

from investment_panel.core.config import load_config
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.payload_archive import provider_archive_path
from investment_panel.database.source_facts import SourceFactRepository
from investment_panel.providers.opencli import OpenCliRateLimitError, OpenCliRunner, ensure_list


NEWS_COMMANDS = {
    "bloomberg": ["bloomberg", "markets"],
    "reuters": ["reuters", "search", "stock market"],
    "google-news": ["google", "news", "stock market"],
    "hackernews": ["hackernews", "top"],
}
TOKEN_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9.]{0,9})|\b([A-Z][A-Z0-9.]{0,9})\b")


def run(config_path: str | None = None, *, kinds: set[str] | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    selected = kinds or {"news", "blogs", "social"}
    runtime = runtime_for_config(config)
    ingestion = IngestionRepository(runtime)
    ingestion.sync_research_source_enablement(
        news_ids=[_slug(f"news_{provider}") for provider in config.research_sources.news.providers],
        blog_sources=[
            *[(_slug(f"blog_{_host(url)}"), "substack") for url in config.research_sources.blogs.substack_urls],
            *[(_slug(f"blog_{_host(url)}"), "rss") for url in config.research_sources.blogs.rss_urls],
        ],
        news_enabled=config.research_sources.news.enabled,
        blogs_enabled=config.research_sources.blogs.enabled,
        x_enabled=bool(config.research_sources.x.enabled and config.research_sources.x.list_id),
    )
    known = _known_symbols(runtime)
    runner = OpenCliRunner(
        command=config.data_sources.opencli.command,
        timeout_seconds=config.data_sources.opencli.timeout_seconds,
    )
    specs: list[dict[str, Any]] = []
    if "news" in selected and config.research_sources.news.enabled:
        specs.extend(
            {
                "source_id": _slug(f"news_{provider}"), "name": provider,
                "kind": "news", "capability": "news", "key": provider,
                "fetch": lambda provider=provider: runner.read_json([
                    *NEWS_COMMANDS[provider], "--limit", str(config.research_sources.news.limit)
                ]),
            }
            for provider in config.research_sources.news.providers
            if provider in NEWS_COMMANDS
        )
    if "blogs" in selected and config.research_sources.blogs.enabled:
        specs.extend(
            {
                "source_id": _slug(f"blog_{_host(url)}"), "name": _host(url),
                "kind": "blog", "capability": "substack", "key": url,
                "fetch": lambda url=url: runner.read_json(["substack", "publication", url]),
            }
            for url in config.research_sources.blogs.substack_urls
        )
        specs.extend(
            {
                "source_id": _slug(f"blog_{_host(url)}"), "name": _host(url),
                "kind": "blog", "capability": "rss", "key": url,
                "fetch": lambda url=url: _fetch_rss(url),
            }
            for url in config.research_sources.blogs.rss_urls
        )
    x = config.research_sources.x
    if "social" in selected and x.enabled and x.list_id:
        specs.append({
            "source_id": "birdclaw_primary_tweets", "name": "Curated X list",
            "kind": "social", "capability": "x_list", "key": x.list_id,
            "fetch": lambda: runner.read_json(["twitter", "list-tweets", str(x.list_id), "--limit", str(x.limit)]),
        })
    results = [_run_source(config, runtime, known, spec) for spec in specs]
    return {
        "status": _overall_status(results),
        "database": "postgresql",
        "items": sum(int(row.get("items") or 0) for row in results),
        "instrument_links": sum(int(row.get("instrument_links") or 0) for row in results),
        "affected_symbols": sorted({
            str(symbol)
            for row in results
            for symbol in row.get("affected_symbols") or []
        }),
        "runs": results,
    }


def run_research(config_path: str | None = None) -> dict[str, Any]:
    return run(config_path, kinds={"news", "blogs"})


def run_social(config_path: str | None = None) -> dict[str, Any]:
    return run(config_path, kinds={"social"})


def _run_source(config: Any, runtime: Any, known: set[str], spec: dict[str, Any]) -> dict[str, Any]:
    repository = IngestionRepository(runtime)
    source_id = str(spec["source_id"])
    repository.register_source(
        source_id,
        name=str(spec["name"]),
        family="social" if spec["kind"] == "social" else "research",
        kind=str(spec["kind"]),
        origin=str(spec["key"]),
        capabilities=(
            {"content": True, str(spec["capability"]): True}
            if source_id == "birdclaw_primary_tweets"
            else {str(spec["capability"]): True}
        ),
    )
    try:
        with repository.run(source_id, str(spec["capability"])) as ingestion_run:
            try:
                payload = spec["fetch"]()
            except OpenCliRateLimitError as exc:
                ingestion_run.finish("partial", failure_detail=str(exc))
                return {
                    "source_id": source_id,
                    "status": "rate_limited",
                    "items": 0,
                    "instrument_links": 0,
                    "error": str(exc),
                }
            raw_rows = ensure_list(payload)
            archive = _archive_payload(config, source_id, ingestion_run.id, payload)
            payload_id = repository.record_payload_file(ingestion_run.id, archive, source_key=str(spec["key"]))
            rows = [_content_row(source_id, spec["kind"], row, known) for row in raw_rows]
            rows = [row for row in rows if row is not None]
            counts = SourceFactRepository(runtime).store_content_items(
                ingestion_run.id, source_id, rows, payload_id=payload_id
            )
            ingestion_run.finish(
                item_count=counts["items"],
                instrument_count=counts["instrument_links"],
                summary={"archive_uri": archive.resolve().as_uri()},
            )
        return {"source_id": source_id, "status": "ok", **counts}
    except Exception as exc:  # every configured source remains independently observable
        return {"source_id": source_id, "status": "failed", "items": 0, "instrument_links": 0, "error": str(exc)}


def _content_row(source_id: str, kind: str, row: dict[str, Any], known: set[str]) -> dict[str, Any] | None:
    title = str(row.get("title") or row.get("headline") or row.get("text") or "").strip()
    if not title:
        return None
    url = row.get("url") or row.get("link")
    summary = str(row.get("summary") or row.get("description") or row.get("text") or "").strip()
    source_key = str(row.get("id") or hashlib.sha256(f"{source_id}|{title}|{url}".encode()).hexdigest())
    published = row.get("published_at") or row.get("published") or row.get("created_at") or row.get("date")
    return {
        "source_key": source_key,
        "kind": kind,
        "title": title[:1000],
        "url": url,
        "author": row.get("author") or row.get("name"),
        "published_at": _timestamp(published),
        "observed_at": datetime.now(UTC),
        "summary": summary[:8000],
        "symbols": _symbols(f"{title} {summary}", known),
        "license_status": "provider_link_only",
        "metadata": {"provider": source_id},
    }


def _known_symbols(runtime: Any) -> set[str]:
    with runtime.read() as connection:
        return {str(row["symbol"]) for row in connection.execute("SELECT symbol FROM catalog.instrument").fetchall()}


def _symbols(text: str, known: set[str]) -> list[str]:
    found: set[str] = set()
    for match in TOKEN_RE.finditer(text):
        symbol = str(match.group(1) or match.group(2) or "").upper()
        # Bare one-letter tokens are prose/punctuation collisions.  Keep the
        # explicit cashtag branch so $C and $F remain valid source evidence.
        if match.group(1) or (len(symbol) > 1 and symbol in known):
            found.add(symbol)
    return sorted(found)


def _archive_payload(config: Any, source_id: str, run_id: Any, payload: Any) -> Path:
    day = datetime.now(UTC).strftime("%Y/%m/%d")
    path = provider_archive_path(config, source_id, day, f"{run_id}.json.gz")
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), default=str)
    return path


def _fetch_rss(url: str) -> list[dict[str, Any]]:
    response = httpx.get(url, timeout=25, headers={"User-Agent": "joehu-market-panel/0.1 contact:local"})
    response.raise_for_status()
    root = ET.fromstring(response.content)
    rows: list[dict[str, Any]] = []
    for node in root.findall(".//item"):
        rows.append({
            "title": _child(node, "title"), "url": _child(node, "link") or _child(node, "guid"),
            "summary": _child(node, "description"), "published": _child(node, "pubDate"),
            "author": _child(node, "author"),
        })
    if rows:
        return rows
    ns = "{http://www.w3.org/2005/Atom}"
    for node in root.findall(f".//{ns}entry"):
        link = node.find(f"{ns}link")
        rows.append({
            "title": _child(node, f"{ns}title"), "url": link.get("href") if link is not None else None,
            "summary": _child(node, f"{ns}summary") or _child(node, f"{ns}content"),
            "published": _child(node, f"{ns}published") or _child(node, f"{ns}updated"),
        })
    return rows


def _child(node: ET.Element, tag: str) -> str:
    child = node.find(tag)
    return str(child.text or "").strip() if child is not None else ""


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _host(url: str) -> str:
    return (urlparse(str(url)).netloc or str(url)).replace("www.", "") or "blog"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "source"


def _overall_status(rows: Iterable[dict[str, Any]]) -> str:
    statuses = {str(row.get("status")) for row in rows}
    if not statuses:
        return "skipped"
    if statuses == {"ok"}:
        return "ok"
    return "failed" if statuses == {"failed"} else "partial"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    print(json.dumps(run(args.config), indent=2, default=str))


if __name__ == "__main__":
    main()
