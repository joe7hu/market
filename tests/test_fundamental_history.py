from __future__ import annotations

import gzip
from hashlib import sha256
import json
from pathlib import Path

from investment_panel.database.fundamental_history import hydrate_history


def test_hydrate_history_reads_a_verified_archive(tmp_path: Path) -> None:
    path = tmp_path / "history.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump([{"date": "2026-01-01", "value": 1}], handle)

    values = hydrate_history({}, archive_uri=path.as_uri(), archive_sha256=sha256(path.read_bytes()).hexdigest())

    assert values["history"] == [{"date": "2026-01-01", "value": 1}]
    assert values["history_data_health"] == {"status": "archived_verified"}


def test_hydrate_history_fails_closed_for_bad_checksum(tmp_path: Path) -> None:
    path = tmp_path / "history.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump([], handle)

    values = hydrate_history({}, archive_uri=path.as_uri(), archive_sha256="0" * 64)

    assert "history" not in values
    assert values["history_data_health"]["status"] == "degraded"
