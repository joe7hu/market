from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from investment_panel.providers.opencli import (
    OpenCliError,
    OpenCliRateLimitError,
    OpenCliRunner,
)


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_read_json_retries_on_rate_limit_then_succeeds(monkeypatch) -> None:
    calls: list[list[str]] = []
    responses = [
        _completed(1, stderr="scanner 429: rate limited"),
        _completed(1, stderr="scanner 429: rate limited"),
        _completed(0, stdout="[{\"symbol\": \"NVDA\"}]"),
    ]

    def fake_run(command, **_kwargs):
        calls.append(command)
        return responses[len(calls) - 1]

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = OpenCliRunner(max_rate_limit_retries=3, rate_limit_backoff_seconds=0)

    result = runner.read_json(["tradingview", "options-chain"])

    assert result == [{"symbol": "NVDA"}]
    assert len(calls) == 3  # two 429s retried, third succeeded


def test_read_json_raises_rate_limit_error_after_exhausting_retries(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return _completed(1, stderr="Too Many Requests")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = OpenCliRunner(max_rate_limit_retries=2, rate_limit_backoff_seconds=0)

    with pytest.raises(OpenCliRateLimitError):
        runner.read_json(["tradingview", "screener"])

    assert len(calls) == 3  # initial attempt + 2 retries


def test_read_json_does_not_retry_non_rate_limit_errors(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return _completed(1, stderr="symbol not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = OpenCliRunner(max_rate_limit_retries=3, rate_limit_backoff_seconds=0)

    with pytest.raises(OpenCliError) as excinfo:
        runner.read_json(["tradingview", "quote"])

    assert not isinstance(excinfo.value, OpenCliRateLimitError)
    assert len(calls) == 1  # no retries for ordinary failures


def test_opencli_resolves_known_installation_when_launchd_path_is_minimal(tmp_path, monkeypatch) -> None:
    candidate = tmp_path / "opencli"
    candidate.write_text("")
    observed: list[list[str]] = []
    monkeypatch.delenv("MARKET_OPENCLI_BIN", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr("investment_panel.core.executable.shutil.which", lambda _name: None)
    monkeypatch.setattr("investment_panel.core.executable.DEFAULT_BIN_DIRS", (str(tmp_path),))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: (
            observed.append([*command, kwargs["env"]["PATH"]])
            or _completed(0, stdout="[]")
        ),
    )

    assert OpenCliRunner().read_json(["twitter", "list-tweets"]) == []
    assert observed[0][0] == str(candidate)
    assert observed[0][-1].split(":", 1)[0] == str(tmp_path)
