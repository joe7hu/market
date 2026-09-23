from __future__ import annotations

from unittest.mock import Mock

import pytest

from investment_panel.infrastructure.postgres.storage_archive import StorageArchiveService


def test_option_expiry_refuses_before_any_file_or_database_action(tmp_path):
    runtime = Mock()
    archive = tmp_path / "evidence.dump"
    archive.write_bytes(b"critical historical evidence")
    service = StorageArchiveService(runtime, tmp_path)
    with pytest.raises(ValueError, match="expiry is disabled"):
        service.expire_option_archives(execute=True)
    assert archive.read_bytes() == b"critical historical evidence"
    assert not runtime.mock_calls
