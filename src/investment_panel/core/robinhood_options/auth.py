"""Robinhood MCP authentication seam.

Everything needed to obtain a usable access token lives here: env/cache lookup,
the browser OAuth + PKCE flow, dynamic client registration, token refresh, and
the Codex MCP credential bridge. The collector depends on this module only
through :func:`load_robinhood_access_token` and :class:`RobinhoodAuthRequired`,
so the OAuth dance can change without touching chain collection.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx


class RobinhoodAuthRequired(RuntimeError):
    """Raised when the runtime has no usable Robinhood MCP access token."""


def load_robinhood_access_token(config: Any) -> str:
    """Load a usable Robinhood MCP access token from env or local OAuth cache."""

    env_name = str(getattr(config, "auth_token_env", "ROBINHOOD_MCP_TOKEN"))
    env_token = os.environ.get(env_name)
    if env_token:
        return env_token
    token_path = _token_path(config)
    if token_path.exists():
        token = _load_access_token_from_payload_path(token_path, config=config, refresh=True)
        if token:
            return token
    if bool(getattr(config, "prefer_codex_credentials", True)):
        token = _load_codex_mcp_access_token(config)
        if token:
            return token
    if not token_path.exists():
        raise RobinhoodAuthRequired(f"Robinhood MCP token cache not found: {token_path}")
    raise RobinhoodAuthRequired(f"Robinhood MCP token cache is expired: {token_path}")


def _load_access_token_from_payload_path(path: Path, *, config: Any, refresh: bool) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RobinhoodAuthRequired(f"Robinhood MCP token cache is unreadable: {path}") from exc
    access_token = payload.get("access_token")
    expires_at = _expiry_seconds(payload.get("expires_at"))
    if isinstance(access_token, str) and access_token and (expires_at is None or expires_at > time.time() + 60):
        return access_token
    if not refresh:
        return None
    refreshed = _refresh_robinhood_token(config, payload)
    if refreshed:
        _write_token_payload(path, refreshed)
        return str(refreshed["access_token"])
    return None


def authorize_robinhood_mcp(config: Any) -> dict[str, Any]:
    """Run a browser OAuth + PKCE flow and persist a local token cache."""

    if bool(getattr(config, "prefer_codex_credentials", True)):
        token = _load_codex_mcp_access_token(config)
        if token:
            return {
                "status": "ok",
                "auth_provider": "codex_mcp",
                "server_name": str(getattr(config, "codex_mcp_server_name", "robinhood-trading")),
                "credentials_path": str(_codex_credentials_path(config)),
            }
        codex_auth = _authorize_with_codex_cli(config)
        if codex_auth.get("status") == "ok":
            return codex_auth

    mcp_url = str(getattr(config, "mcp_url", "https://agent.robinhood.com/mcp/trading"))
    resource_metadata_url = _discover_resource_metadata_url(mcp_url, int(getattr(config, "timeout_seconds", 30)))
    resource_metadata = _get_json(resource_metadata_url, timeout=int(getattr(config, "timeout_seconds", 30)))
    resource = str(resource_metadata.get("resource") or mcp_url)
    auth_servers = resource_metadata.get("authorization_servers") or []
    if not auth_servers:
        raise RuntimeError("Robinhood MCP protected-resource metadata did not advertise an authorization server")
    auth_server = str(auth_servers[0])
    auth_metadata = _authorization_server_metadata(auth_server, timeout=int(getattr(config, "timeout_seconds", 30)))
    client_id = str(getattr(config, "client_id", "") or "")
    callback_host = str(getattr(config, "callback_host", "127.0.0.1"))
    callback_port = int(getattr(config, "callback_port", 8765))
    redirect_uri = f"http://{callback_host}:{callback_port}/callback"
    if not client_id:
        client_id = _register_oauth_client(auth_metadata, redirect_uri, timeout=int(getattr(config, "timeout_seconds", 30)))
    verifier = _pkce_verifier()
    challenge = _pkce_challenge(verifier)
    state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "resource": resource,
    }
    scope = str(getattr(config, "scope", "") or "")
    if scope:
        params["scope"] = scope
    authorization_endpoint = str(auth_metadata.get("authorization_endpoint") or "")
    token_endpoint = str(auth_metadata.get("token_endpoint") or "")
    if not authorization_endpoint or not token_endpoint:
        raise RuntimeError("Robinhood MCP authorization server metadata is missing authorization or token endpoint")
    auth_url = f"{authorization_endpoint}?{urlencode(params)}"
    code = _wait_for_oauth_callback(auth_url, expected_state=state, host=callback_host, port=callback_port)
    token_payload = _exchange_authorization_code(
        token_endpoint,
        client_id=client_id,
        code=code,
        redirect_uri=redirect_uri,
        verifier=verifier,
        resource=resource,
        timeout=int(getattr(config, "timeout_seconds", 30)),
    )
    token_payload["client_id"] = client_id
    token_payload["token_endpoint"] = token_endpoint
    token_payload["resource"] = resource
    token_payload["saved_at"] = time.time()
    token_path = _token_path(config)
    _write_token_payload(token_path, token_payload)
    return {"status": "ok", "token_path": str(token_path), "expires_at": token_payload.get("expires_at")}


def _discover_resource_metadata_url(mcp_url: str, timeout: int) -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "market-robinhood-provider", "version": "0.1.0"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    response = httpx.post(mcp_url, headers=headers, json=payload, timeout=timeout)
    header = response.headers.get("www-authenticate") or response.headers.get("WWW-Authenticate") or ""
    metadata_url = _www_authenticate_param(header, "resource_metadata")
    if metadata_url:
        return metadata_url
    parsed = urlparse(mcp_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return f"{origin}/.well-known/oauth-protected-resource"


def _authorization_server_metadata(auth_server: str, *, timeout: int) -> dict[str, Any]:
    for url in _authorization_server_metadata_urls(auth_server):
        try:
            payload = _get_json(url, timeout=timeout)
        except httpx.HTTPError:
            continue
        if payload.get("authorization_endpoint") and payload.get("token_endpoint"):
            return payload
    raise RuntimeError(f"Authorization server metadata was not available for {auth_server}")


def _authorization_server_metadata_urls(auth_server: str) -> list[str]:
    parsed = urlparse(auth_server)
    base = auth_server.rstrip("/")
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else base
    issuer_path = parsed.path.rstrip("/")
    candidates = [
        f"{base}/.well-known/openid-configuration",
        f"{base}/.well-known/oauth-authorization-server",
    ]
    if origin and issuer_path:
        candidates.extend(
            [
                f"{origin}/.well-known/openid-configuration{issuer_path}",
                f"{origin}/.well-known/oauth-authorization-server{issuer_path}",
            ]
        )
    if origin:
        candidates.extend(
            [
                f"{origin}/.well-known/openid-configuration",
                f"{origin}/.well-known/oauth-authorization-server",
            ]
        )
    return list(dict.fromkeys(candidates))


def _register_oauth_client(auth_metadata: dict[str, Any], redirect_uri: str, *, timeout: int) -> str:
    endpoint = str(auth_metadata.get("registration_endpoint") or "")
    if not endpoint:
        raise RuntimeError("Authorization server does not support dynamic client registration; configure robinhood.client_id")
    payload = {
        "client_name": "Market Robinhood Provider",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    response = httpx.post(endpoint, json=payload, timeout=timeout)
    response.raise_for_status()
    registered = response.json()
    client_id = registered.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise RuntimeError("Dynamic client registration did not return client_id")
    return client_id


def _exchange_authorization_code(
    token_endpoint: str,
    *,
    client_id: str,
    code: str,
    redirect_uri: str,
    verifier: str,
    resource: str,
    timeout: int,
) -> dict[str, Any]:
    payload = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "resource": resource,
    }
    response = httpx.post(token_endpoint, data=payload, timeout=timeout)
    response.raise_for_status()
    token_payload = response.json()
    return _token_payload_with_expiry(token_payload)


def _refresh_robinhood_token(config: Any, payload: dict[str, Any]) -> dict[str, Any] | None:
    refresh_token = payload.get("refresh_token")
    token_endpoint = payload.get("token_endpoint") or _robinhood_token_endpoint(config)
    client_id = payload.get("client_id") or getattr(config, "client_id", None)
    resource = payload.get("resource") or getattr(config, "mcp_url", None)
    if not refresh_token or not token_endpoint or not client_id:
        return None
    response = httpx.post(
        str(token_endpoint),
        data={
            "grant_type": "refresh_token",
            "client_id": str(client_id),
            "refresh_token": str(refresh_token),
            "resource": str(resource),
        },
        timeout=int(getattr(config, "timeout_seconds", 30)),
    )
    if response.status_code >= 400:
        return None
    refreshed = _token_payload_with_expiry(response.json())
    refreshed.setdefault("refresh_token", refresh_token)
    refreshed["client_id"] = client_id
    refreshed["token_endpoint"] = token_endpoint
    refreshed["resource"] = resource
    refreshed["saved_at"] = time.time()
    return refreshed


def _load_codex_mcp_access_token(config: Any) -> str | None:
    payload = _codex_mcp_credential(config)
    if not payload:
        return None
    access_token = payload.get("access_token")
    expires_at = _expiry_seconds(payload.get("expires_at"))
    if isinstance(access_token, str) and access_token and (expires_at is None or expires_at > time.time() + 60):
        return access_token
    refreshed = _refresh_robinhood_token(config, payload)
    if not refreshed:
        return None
    _write_codex_mcp_credential(config, refreshed)
    return str(refreshed["access_token"])


def _codex_mcp_credential(config: Any) -> dict[str, Any] | None:
    path = _codex_credentials_path(config)
    try:
        credentials = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(credentials, dict):
        return None
    server_name = str(getattr(config, "codex_mcp_server_name", "robinhood-trading"))
    server_url = str(getattr(config, "mcp_url", "https://agent.robinhood.com/mcp/trading"))
    for value in credentials.values():
        if not isinstance(value, dict):
            continue
        if value.get("server_name") == server_name or value.get("server_url") == server_url:
            payload = dict(value)
            payload.setdefault("resource", server_url)
            return payload
    return None


def _write_codex_mcp_credential(config: Any, refreshed: dict[str, Any]) -> None:
    path = _codex_credentials_path(config)
    try:
        credentials = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(credentials, dict):
        return
    server_name = str(getattr(config, "codex_mcp_server_name", "robinhood-trading"))
    server_url = str(getattr(config, "mcp_url", "https://agent.robinhood.com/mcp/trading"))
    for key, value in credentials.items():
        if not isinstance(value, dict):
            continue
        if value.get("server_name") != server_name and value.get("server_url") != server_url:
            continue
        updated = dict(value)
        updated.update(refreshed)
        updated.setdefault("server_name", server_name)
        updated.setdefault("server_url", server_url)
        expires_at = _expiry_seconds(updated.get("expires_at"))
        if expires_at is not None:
            updated["expires_at"] = int(expires_at * 1000)
        credentials[key] = updated
        path.write_text(json.dumps(credentials, indent=2), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return


def _authorize_with_codex_cli(config: Any) -> dict[str, Any]:
    codex = shutil.which("codex")
    if not codex:
        return {"status": "unavailable", "auth_provider": "codex_mcp", "reason": "codex CLI was not found"}
    server_name = str(getattr(config, "codex_mcp_server_name", "robinhood-trading"))
    scope = str(getattr(config, "scope", "internal") or "")
    command = [codex, "mcp", "login"]
    if scope:
        command.extend(["--scopes", scope])
    command.append(server_name)
    subprocess.run(command, check=True)
    token = _load_codex_mcp_access_token(config)
    if not token:
        raise RobinhoodAuthRequired(f"Codex MCP login completed but no usable credential was found for {server_name}")
    return {
        "status": "ok",
        "auth_provider": "codex_mcp",
        "server_name": server_name,
        "credentials_path": str(_codex_credentials_path(config)),
    }


def _robinhood_token_endpoint(config: Any) -> str | None:
    try:
        timeout = int(getattr(config, "timeout_seconds", 30))
        resource_metadata_url = _discover_resource_metadata_url(str(getattr(config, "mcp_url")), timeout)
        resource_metadata = _get_json(resource_metadata_url, timeout=timeout)
        auth_server = str((resource_metadata.get("authorization_servers") or [""])[0])
        if not auth_server:
            return None
        metadata = _authorization_server_metadata(auth_server, timeout=timeout)
    except Exception:
        return None
    endpoint = metadata.get("token_endpoint")
    return str(endpoint) if endpoint else None


def _wait_for_oauth_callback(auth_url: str, *, expected_state: str, host: str, port: int) -> str:
    box: dict[str, str] = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            state = params.get("state", [""])[0]
            code = params.get("code", [""])[0]
            error = params.get("error", [""])[0]
            if error:
                box["error"] = error
            elif state != expected_state:
                box["error"] = "OAuth state mismatch"
            elif code:
                box["code"] = code
            self.send_response(200 if "code" in box else 400)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            message = "Robinhood authorization complete. You can close this tab." if "code" in box else f"Authorization failed: {box.get('error')}"
            self.wfile.write(message.encode("utf-8"))

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = HTTPServer((host, port), _Handler)
    print(f"Open this URL to authorize Robinhood MCP:\n{auth_url}")
    webbrowser.open(auth_url)
    server.handle_request()
    server.server_close()
    if box.get("code"):
        return box["code"]
    raise RuntimeError(box.get("error") or "OAuth callback did not include a code")


def _get_json(url: str, *, timeout: int) -> dict[str, Any]:
    response = httpx.get(url, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    return dict(payload) if isinstance(payload, dict) else {}


def _www_authenticate_param(header: str, key: str) -> str | None:
    marker = f'{key}="'
    start = header.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = header.find('"', start)
    return header[start:end] if end >= start else None


def _pkce_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _token_payload_with_expiry(payload: dict[str, Any]) -> dict[str, Any]:
    token_payload = dict(payload)
    expires_in = _float_value(token_payload.get("expires_in"))
    if expires_in is not None:
        token_payload["expires_at"] = time.time() + expires_in
    return token_payload


def _token_path(config: Any) -> Path:
    return Path(os.path.expandvars(str(getattr(config, "token_path", "~/.config/market/robinhood-mcp-token.json")))).expanduser()


def _codex_credentials_path(config: Any) -> Path:
    return Path(os.path.expandvars(str(getattr(config, "codex_credentials_path", "~/.codex/.credentials.json")))).expanduser()


def _write_token_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _float_value(value: Any) -> float | None:
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _expiry_seconds(value: Any) -> float | None:
    expires_at = _float_value(value)
    if expires_at is None:
        return None
    return expires_at / 1000 if expires_at > 10_000_000_000 else expires_at
