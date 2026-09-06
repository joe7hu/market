from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

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


@pytest.mark.parametrize('collector', ['content', 'events', 'phase2'])
def test_collector_archives_reuse_identical_bytes_across_runs(tmp_path: Path, collector: str) -> None:
    import gzip
    from hashlib import sha256
    import json
    from investment_panel.jobs import update_content_sources, update_market_events, update_phase2_sources

    config = SimpleNamespace(
        nas=SimpleNamespace(market_dir=tmp_path / 'absent' / 'nas'),
        report_dir=tmp_path / 'local' / 'reports',
    )
    writers = {
        'content': lambda run, body: update_content_sources._archive_payload(config, 'test', run, body),
        'events': lambda run, body: update_market_events._archive_payload(config, run, body),
        'phase2': lambda run, body: update_phase2_sources._archive_payload(config, 'test', run, body),
    }
    write = writers[collector]
    first = write('run-1', {'value': 1, 'name': 'same'})
    first_bytes = first.read_bytes()
    first_mtime = first.stat().st_mtime_ns
    second = write('run-2', {'name': 'same', 'value': 1})
    changed = write('run-3', {'value': 2, 'name': 'same'})
    assert first == second != changed
    assert first.stat().st_mtime_ns == first_mtime
    assert first.read_bytes() == first_bytes
    assert first.name.split('.')[0] == sha256(first_bytes).hexdigest()
    raw = gzip.decompress(first_bytes) if collector != 'phase2' else first_bytes
    assert json.loads(raw) == {'value': 1, 'name': 'same'}
    assert len(list((tmp_path / 'local' / 'provider-payloads').rglob('*.json*'))) == 2
    first.write_bytes(b'corrupted')
    with pytest.raises(ValueError, match='digest mismatch'):
        write('run-4', {'name': 'same', 'value': 1})
    assert first.read_bytes() == b'corrupted'
