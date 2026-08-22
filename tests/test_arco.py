from __future__ import annotations

import json
from pathlib import Path

from investment_panel.core.arco import arco_readability_preflight
from investment_panel.core.config import ArcoConfig


def test_arco_readability_preflight_accepts_required_json_files(tmp_path: Path) -> None:
    (tmp_path / "signals.json").write_text(json.dumps({"topics": []}), encoding="utf-8")
    (tmp_path / "beliefs.json").write_text(json.dumps({"beliefs": []}), encoding="utf-8")

    result = arco_readability_preflight(ArcoConfig(raw_dir=tmp_path))

    assert result["ok"] is True
    assert result["status"] == "readable"


def test_arco_readability_preflight_classifies_missing_and_unmounted_paths(tmp_path: Path) -> None:
    missing = arco_readability_preflight(ArcoConfig(raw_dir=tmp_path / "missing"))
    unmounted = arco_readability_preflight(
        ArcoConfig(raw_dir=Path("/Volumes/agent/does-not-exist/arco"))
    )

    assert missing["status"] == "missing"
    assert unmounted["status"] == "unmounted"


def test_arco_readability_preflight_classifies_permission_denied(tmp_path: Path, monkeypatch) -> None:
    signals = tmp_path / "signals.json"
    beliefs = tmp_path / "beliefs.json"
    signals.write_text(json.dumps({"topics": []}), encoding="utf-8")
    beliefs.write_text(json.dumps({"beliefs": []}), encoding="utf-8")
    original_open = Path.open

    def deny_signals(path: Path, *args, **kwargs):
        if path == signals:
            raise PermissionError("SMB access denied")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_signals)

    result = arco_readability_preflight(ArcoConfig(raw_dir=tmp_path))

    assert result["ok"] is False
    assert result["status"] == "permission_denied"
    assert "SMB access denied" in result["error"]
