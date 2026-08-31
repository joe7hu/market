from __future__ import annotations

from datetime import UTC, datetime
import socket

from defusedxml.common import EntitiesForbidden
import pytest

from app.data_access import settings as settings_owner
from conftest import typed_config
from investment_panel.core import config as config_owner
from investment_panel.core import settings_validation
from investment_panel.core.settings_validation import (
    apply_agent_settings_update,
    apply_research_sources_update,
)
from investment_panel.jobs import update_content_sources, update_disclosure_sources


def test_load_config_ignores_legacy_poisoned_agent_override(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """agents:
  option_agent:
    provider: codex
    command: market-run-option-agent
    timeout_seconds: 60
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        config_owner,
        "persisted_setting_sections",
        lambda _database_url: {
            "agents": {
                "option_agent": {
                    "provider": "codex",
                    "command": "/bin/sh",
                    "timeout_seconds": -1,
                }
            }
        },
    )

    config = config_owner.load_config(config_path)

    assert config.agents.option_agent.command == "market-run-option-agent"
    assert config.agents.option_agent.timeout_seconds == 60


def test_agent_setting_update_rejects_config_poison_before_persistence() -> None:
    config = typed_config()

    with pytest.raises(ValueError, match="command is registry-owned"):
        settings_owner.persist_setting_section(
            config,
            "agents",
            {"option_agent": {"provider": "codex", "command": "/bin/sh"}},
        )
    with pytest.raises(ValueError, match="timeout_seconds must be between 10 and 900"):
        settings_owner.persist_setting_section(
            config,
            "agents",
            {"option_agent": {"timeout_seconds": -1}},
        )


def test_agent_setting_update_preserves_non_editable_fields() -> None:
    current = {
        "option_agent": {
            "provider": "codex",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "high",
            "command": "market-run-option-agent",
            "experiment_enabled": True,
        },
        "pricing": {"default": {"input_per_1m": 1.25}},
    }

    result = apply_agent_settings_update(
        current,
        {
            "option_agent": {
                "timeout_seconds": 90,
                "experiment_enabled": False,
                "unrecognized_setting": "poison",
            }
        },
    )

    assert result["option_agent"]["timeout_seconds"] == 90
    assert result["option_agent"]["experiment_enabled"] is True
    assert "unrecognized_setting" not in result["option_agent"]
    assert result["pricing"] == current["pricing"]


def test_research_setting_update_rejects_private_and_dns_resolved_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="private or non-routable"):
        apply_research_sources_update(
            {},
            {"blogs": {"rss_urls": ["http://127.0.0.1:5432/private"]}},
        )

    monkeypatch.setattr(
        settings_validation.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 80))
        ],
    )
    with pytest.raises(ValueError, match="private or non-routable"):
        apply_research_sources_update(
            {},
            {"blogs": {"rss_urls": ["https://feed.example.test/rss"]}},
        )


def test_research_setting_update_normalizes_public_url_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings_validation.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )

    result = apply_research_sources_update(
        {},
        {
            "blogs": {
                "rss_urls": "https://feed.example.test/rss, https://feed.example.test/rss",
            }
        },
    )

    assert result["blogs"]["rss_urls"] == ["https://feed.example.test/rss"]


def test_rss_fetch_revalidates_legacy_private_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def unexpected_get(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("private URL reached the HTTP client")

    monkeypatch.setattr(update_content_sources.httpx.Client, "send", unexpected_get)

    with pytest.raises(ValueError, match="private or non-routable"):
        update_content_sources._fetch_rss("http://127.0.0.1:5432/private")
    assert called is False


def test_substack_fetch_revalidates_legacy_private_url() -> None:
    with pytest.raises(ValueError, match="private or non-routable"):
        update_content_sources._fetch_substack("http://127.0.0.1:5432/private")


def test_external_entities_are_rejected_by_content_parsers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"""<!DOCTYPE rss [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]>
<rss><channel><item><title>&xxe;</title></item></channel></rss>"""

    class Response:
        content = payload
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

    monkeypatch.setattr(
        update_content_sources,
        "resolve_public_http_url",
        lambda url: settings_validation.ResolvedPublicHttpUrl(
            url=url,
            hostname="feed.example.test",
            authority="feed.example.test",
            address="93.184.216.34",
        ),
    )
    request_options = {}

    def get_response(_client, _request, **kwargs):
        request_options.update(kwargs)
        return Response()

    monkeypatch.setattr(update_content_sources.httpx.Client, "send", get_response)

    with pytest.raises(EntitiesForbidden):
        update_content_sources._fetch_rss("https://feed.example.test/rss")
    assert request_options["follow_redirects"] is False
    with pytest.raises(EntitiesForbidden):
        update_disclosure_sources._parse_information_table(payload)


def test_rss_fetch_pins_validated_address_and_original_tls_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolutions = 0

    def resolve(*_args, **_kwargs):
        nonlocal resolutions
        resolutions += 1
        address = "93.184.216.34" if resolutions == 1 else "10.0.0.8"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    def send(_client, request, **kwargs):
        assert str(request.url) == "https://93.184.216.34/rss"
        assert request.headers["Host"] == "feed.example.test"
        assert request.extensions["sni_hostname"] == "feed.example.test"
        assert kwargs["follow_redirects"] is False
        return update_content_sources.httpx.Response(
            200,
            request=request,
            content=b"<rss><channel><item><title>Safe</title></item></channel></rss>",
        )

    monkeypatch.setattr(settings_validation.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(update_content_sources.httpx.Client, "send", send)

    assert update_content_sources._fetch_rss("https://feed.example.test/rss")[0]["title"] == "Safe"
    assert resolutions == 1


def test_rss_fetch_rejects_private_redirect_before_second_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sends = 0

    monkeypatch.setattr(
        settings_validation.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )

    def send(_client, request, **_kwargs):
        nonlocal sends
        sends += 1
        return update_content_sources.httpx.Response(
            302,
            request=request,
            headers={"Location": "http://127.0.0.1/private"},
        )

    monkeypatch.setattr(update_content_sources.httpx.Client, "send", send)

    with pytest.raises(ValueError, match="private or non-routable"):
        update_content_sources._fetch_rss("https://feed.example.test/rss")
    assert sends == 1


def test_rss_fetch_follows_validated_pinned_redirect_without_header_leakage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    addresses = {
        "feed.example.test": "93.184.216.34",
        "rss.example.test": "151.101.1.69",
    }
    requests = []

    def resolve(host, *_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addresses[host], 443))]

    def send(client, request, **kwargs):
        requests.append(request)
        assert kwargs["follow_redirects"] is False
        assert "Authorization" not in request.headers
        assert "Cookie" not in request.headers
        if len(requests) == 1:
            client.cookies.set("session", "secret", domain=".example.test")
            return update_content_sources.httpx.Response(
                302,
                request=request,
                headers={"Location": "https://rss.example.test/final-feed"},
            )
        return update_content_sources.httpx.Response(
            200,
            request=request,
            content=b"<rss><channel><item><title>Redirected</title></item></channel></rss>",
        )

    monkeypatch.setattr(settings_validation.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(update_content_sources.httpx.Client, "send", send)

    assert update_content_sources._fetch_rss("https://feed.example.test/rss")[0]["title"] == "Redirected"
    assert [str(request.url) for request in requests] == [
        "https://93.184.216.34/rss",
        "https://151.101.1.69/final-feed",
    ]
    assert [request.headers["Host"] for request in requests] == [
        "feed.example.test", "rss.example.test",
    ]
    assert [request.extensions["sni_hostname"] for request in requests] == [
        "feed.example.test", "rss.example.test",
    ]


def test_rss_fetch_rejects_redirect_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    sends = 0
    monkeypatch.setattr(
        settings_validation.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )

    def send(_client, request, **_kwargs):
        nonlocal sends
        sends += 1
        return update_content_sources.httpx.Response(
            302, request=request, headers={"Location": "/rss#again"},
        )

    monkeypatch.setattr(update_content_sources.httpx.Client, "send", send)

    with pytest.raises(ValueError, match="redirect loop"):
        update_content_sources._fetch_rss("https://feed.example.test/rss")
    assert sends == 1


def test_rss_fetch_enforces_redirect_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    sends = 0
    monkeypatch.setattr(
        settings_validation.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )

    def send(_client, request, **_kwargs):
        nonlocal sends
        sends += 1
        return update_content_sources.httpx.Response(
            302, request=request, headers={"Location": f"/redirect-{sends}"},
        )

    monkeypatch.setattr(update_content_sources.httpx.Client, "send", send)

    with pytest.raises(ValueError, match="exceeded 5 redirects"):
        update_content_sources._fetch_rss("https://feed.example.test/rss")
    assert sends == update_content_sources.MAX_RSS_REDIRECTS + 1


@pytest.mark.parametrize(
    ("location", "message"),
    [(None, "missing Location"), ("http://[::1", "Location is invalid")],
)
def test_rss_fetch_rejects_missing_or_invalid_redirect_location(
    monkeypatch: pytest.MonkeyPatch,
    location: str | None,
    message: str,
) -> None:
    monkeypatch.setattr(
        settings_validation.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )

    def send(_client, request, **_kwargs):
        return update_content_sources.httpx.Response(
            302,
            request=request,
            headers={"Location": location} if location is not None else {},
        )

    monkeypatch.setattr(update_content_sources.httpx.Client, "send", send)

    with pytest.raises(ValueError, match=message):
        update_content_sources._fetch_rss("https://feed.example.test/rss")


def test_substack_uses_the_pinned_rss_fetch_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []
    monkeypatch.setattr(
        update_content_sources,
        "_fetch_rss",
        lambda url: requested.append(url) or [{"title": "Safe"}],
    )

    assert update_content_sources._fetch_substack("https://notes.example.test") == [
        {"title": "Safe"}
    ]
    assert requested == ["https://notes.example.test/feed"]


def test_rss_pubdate_preserves_source_time_and_malformed_values_fail_closed() -> None:
    row = update_content_sources._content_row(
        "blog_notes",
        "blog",
        {"title": "Source item", "published": "Tue, 26 Aug 2025 15:01:30 GMT"},
        set(),
    )

    assert row is not None
    assert row["published_at"] == datetime(2025, 8, 26, 15, 1, 30, tzinfo=UTC)
    assert row["observed_at"].tzinfo is UTC
    assert update_content_sources._timestamp("2025-08-26T11:01:30-04:00") == row["published_at"]
    assert update_content_sources._timestamp("not a valid feed timestamp") is None
