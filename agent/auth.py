"""Authentication helpers for the Choreo-hosted MCP servers.

Choreo protects APIs with OAuth2 bearer tokens. This module supports two modes:

* A static token (``*_ACCESS_TOKEN``), used as-is.
* OAuth2 client credentials (``*_CLIENT_ID`` / ``*_CLIENT_SECRET``), where a token
  is fetched from the token endpoint and refreshed before it expires or after a 401.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Generator

import httpx

DEFAULT_TOKEN_URL = "https://sts.choreo.dev/oauth2/token"


class ClientCredentialsAuth(httpx.Auth):
    """httpx auth flow that attaches a client-credentials bearer token."""

    requires_response_body = False

    def __init__(self, client_id: str, client_secret: str, token_url: str, scope: str | None = None):
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url
        self._scope = scope
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def _fetch_token(self) -> str:
        data = {"grant_type": "client_credentials"}
        if self._scope:
            data["scope"] = self._scope
        resp = httpx.post(
            self._token_url,
            data=data,
            auth=(self._client_id, self._client_secret),
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        # Refresh a minute early to avoid using a token right as it expires.
        self._expires_at = time.time() + int(body.get("expires_in", 3600)) - 60
        return self._token

    def _get_token(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._token or time.time() >= self._expires_at:
                return self._fetch_token()
            return self._token

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._get_token()}"
        response = yield request
        if response.status_code == 401:
            request.headers["Authorization"] = f"Bearer {self._get_token(force=True)}"
            yield request


def build_auth(prefix: str) -> tuple[dict[str, str], httpx.Auth | None]:
    """Build headers/auth for an MCP server from ``{prefix}_*`` environment variables.

    Falls back to the shared ``CHOREO_*`` variables when server-specific ones are unset.
    Returns ``(headers, auth)``; both are empty/None when no credentials are configured.
    """

    def env(name: str) -> str | None:
        return os.getenv(f"{prefix}_{name}") or os.getenv(f"CHOREO_{name}") or None

    headers: dict[str, str] = {}
    if api_key := env("API_KEY"):
        headers["api-key"] = api_key

    if token := env("ACCESS_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
        return headers, None

    client_id, client_secret = env("CLIENT_ID"), env("CLIENT_SECRET")
    if client_id and client_secret:
        auth = ClientCredentialsAuth(
            client_id,
            client_secret,
            token_url=env("TOKEN_URL") or DEFAULT_TOKEN_URL,
            scope=env("SCOPE"),
        )
        return headers, auth

    return headers, None
