from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from investment_panel.database import payload_archive


def test_provider_archive_path_falls_back_when_nas_is_not_writable(tmp_path: Path, monkeypatch) -> None:
    preferred = tmp_path / "nas" / "provider-payloads"
    fallback = tmp_path / "local" / "provider-payloads"
    preferred.parent.mkdir()
    config = SimpleNamespace(
        nas=SimpleNamespace(market_dir=preferred.parent),
        report_dir=fallback.parent / "reports",
    )
    monkeypatch.setattr(payload_archive, "_root_is_writable", lambda root: root == fallback)

    path = payload_archive.provider_archive_path(config, "news_reuters", "payload.json.gz")

    assert path == fallback / "news_reuters" / "payload.json.gz"


def test_provider_archive_path_does_not_create_an_absent_nas_tree(tmp_path: Path) -> None:
    preferred = tmp_path / "unmounted" / "market-mini" / "provider-payloads"
    fallback = tmp_path / "local" / "provider-payloads"
    config = SimpleNamespace(
        nas=SimpleNamespace(market_dir=preferred.parent),
        report_dir=fallback.parent / "reports",
    )

    path = payload_archive.provider_archive_path(config, "news_reuters", "payload.json.gz")

    assert path == fallback / "news_reuters" / "payload.json.gz"
    assert not preferred.parent.exists()
