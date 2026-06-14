"""OAuth/JWT token resolution for the OpenAI/Codex option-agent commands.

Split out of ``openai_option_agent`` to keep that module within the size budget.
These helpers are pure token plumbing (no network, no agent logic): they locate a
Codex OAuth access token with the ``api.responses.write`` scope from the local
Codex ``auth.json`` so the worker can call the OpenAI Responses API.
"""

from __future__ import annotations

import json
import os
import time
from base64 import urlsafe_b64decode
from binascii import Error as BinasciiError
from pathlib import Path
from typing import Any


def codex_oauth_access_token() -> str:
    auth_path = Path(os.environ.get("MARKET_OPENAI_OAUTH_FILE") or (Path.home() / ".codex" / "auth.json"))
    try:
        data = json.loads(auth_path.read_text())
    except (OSError, json.JSONDecodeError):
        return ""

    candidates: list[str] = []
    _collect_access_tokens(data, candidates)
    if not candidates:
        return ""

    now = int(time.time())
    for token in candidates:
        payload = _jwt_payload(token)
        if not payload:
            continue
        exp = payload.get("exp")
        aud = payload.get("aud")
        audiences = aud if isinstance(aud, list) else [aud]
        is_openai_api = "https://api.openai.com/v1" in audiences
        is_current = not isinstance(exp, (int, float)) or exp > now + 60
        if is_openai_api and is_current and _has_responses_write_scope(payload):
            return token
    return ""


def _has_responses_write_scope(payload: dict[str, Any]) -> bool:
    scopes = payload.get("scp", payload.get("scope"))
    if isinstance(scopes, str):
        scope_values = scopes.split()
    elif isinstance(scopes, list):
        scope_values = [str(scope) for scope in scopes]
    else:
        scope_values = []
    return "api.responses.write" in set(scope_values)


def _collect_access_tokens(value: Any, candidates: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "access_token" and isinstance(child, str) and child:
                candidates.append(child)
            else:
                _collect_access_tokens(child, candidates)
    elif isinstance(value, list):
        for child in value:
            _collect_access_tokens(child, candidates)


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = urlsafe_b64decode(payload.encode())
        data = json.loads(decoded)
    except (BinasciiError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
