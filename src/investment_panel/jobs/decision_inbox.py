"""Synchronize actionable decision events and the fixed-owner notification outbox."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from investment_panel.core.config import load_config
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.decision_inbox import DecisionInboxRepository


def run(config_path: str | None = "config.yaml") -> dict[str, Any]:
    config = load_config(config_path)
    settings = config.analysis.options_decision_system
    if not settings.decision_inbox_enabled:
        return {"status": "skipped", "reason": "decision_inbox_enabled_false"}
    repository = DecisionInboxRepository(runtime_for_config(config))
    synced = repository.sync_current_tickets()
    if not settings.telegram_notifications_enabled:
        return {"status": "ok", "synced": synced, "delivery": {"skipped": 1, "reason": "telegram_notifications_enabled_false"}}
    dry_run = bool(settings.telegram_notifications_dry_run)
    sender: Callable[[str], None] | None = None if dry_run else _fixed_owner_sender()
    delivery = repository.deliver_outbox(sender=sender, dry_run=dry_run)
    return {"status": "ok", "synced": synced, "delivery": delivery, "telegram_dry_run": dry_run}


def _fixed_owner_sender() -> Callable[[str], None]:
    """Return a relay sender without storing recipient IDs or bot credentials.

    The shared GBrain owner relay is deliberately configured outside Market.  A
    local relay endpoint receives only a compact message and resolves the fixed
    owner chat itself.  This process never reads a recipient, token, or chat ID.
    """

    endpoint = os.environ.get("MARKET_GBRAIN_TELEGRAM_OWNER_RELAY_URL", "").strip()
    if endpoint:
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise RuntimeError("shared GBrain fixed-owner relay URL is not configured as a local relay")

        def send(message: str) -> None:
            request = Request(
                endpoint,
                data=json.dumps({"message": message}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=10) as response:  # nosec B310 - local relay is validated above
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"shared GBrain owner relay returned {response.status}")

        return send

    command_text = os.environ.get("MARKET_GBRAIN_TELEGRAM_OWNER_RELAY_COMMAND", "").strip()
    command = shlex.split(command_text)
    if command and not os.path.isabs(command[0]):
        raise RuntimeError("shared GBrain owner relay command must use an absolute path")
    if command:
        def send(message: str) -> None:
            result = subprocess.run(
                command,
                input=message,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
            if result.returncode:
                detail = (result.stderr or result.stdout).strip()
                raise RuntimeError(f"shared GBrain owner relay command failed: {detail[:500]}")

        return send

    raise RuntimeError("shared GBrain fixed-owner relay is not configured")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    print(json.dumps(run(args.config), default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
