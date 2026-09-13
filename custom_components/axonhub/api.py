"""Async client for the AxonHub HTTP API.

AxonHub exposes two authenticated surfaces:

* ``POST /admin/auth/signin`` – email/password login returning a JWT that is
  valid for 7 days.
* ``POST /admin/graphql`` – the admin GraphQL endpoint, authenticated with that
  JWT. This is the only surface that exposes provider quota information; the
  API-key based ``/openapi/v1/graphql`` endpoint does not expose any read query.

This module therefore signs in with the credentials stored in the config entry,
caches the JWT and transparently re-authenticates when it expires.

Every failure carries the response AxonHub actually sent (GraphQL error
messages, the HTTP status and a snippet of a non-JSON body) and is logged at
WARNING level, so a misconfiguration is diagnosable from the Home Assistant log
instead of a bare "AxonHub returned an error".
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from http import HTTPStatus
import json as json_module
import logging
from typing import Any
from urllib.parse import urlparse, urlunparse

import aiohttp
from homeassistant.util import dt as dt_util

from .const import (
    CHECK_TIMEOUT,
    REQUEST_TIMEOUT,
    TOKEN_LIFETIME,
    TOKEN_REFRESH_MARGIN,
)

_LOGGER = logging.getLogger(__name__)

SIGN_IN_PATH = "/admin/auth/signin"
GRAPHQL_PATH = "/admin/graphql"

# How much of an unexpected response body to quote in an error message.
_BODY_SNIPPET_LIMIT = 200

# The same query the AxonHub web UI uses to render provider quota badges.
# `queryChannels` returns every channel unpaginated as long as neither `first`
# nor `last` is set.
CHANNEL_QUOTA_QUERY = """
query HomeAssistantProviderQuotas($input: QueryChannelInput!) {
  queryChannels(input: $input) {
    edges {
      node {
        id
        name
        type
        providerQuotaStatus {
          status
          nextResetAt
          nextCheckAt
          ready
          quotaData
        }
      }
    }
  }
}
"""

# Forces AxonHub to re-check every channel against its provider right away
# instead of waiting for the periodic (default 20m) background check.
TRIGGER_CHECK_MUTATION = """
mutation HomeAssistantRefreshProviderQuotas {
  checkProviderQuotas
}
"""


class AxonHubError(Exception):
    """Base class for AxonHub API errors."""


class AxonHubAuthError(AxonHubError):
    """Raised when AxonHub rejects the credentials or the JWT is no longer valid."""


class AxonHubConnectionError(AxonHubError):
    """Raised when AxonHub cannot be reached."""


class AxonHubApiError(AxonHubError):
    """Raised when AxonHub answers with an error."""


def normalize_base_url(base_url: str) -> str:
    """Normalize a user supplied base URL to ``scheme://host[:port][/path]``."""
    candidate = (base_url or "").strip()
    if not candidate:
        raise AxonHubConnectionError("An AxonHub base URL is required")
    if "://" not in candidate:
        candidate = f"http://{candidate}"

    parsed = urlparse(candidate)
    if not parsed.scheme or not parsed.netloc:
        raise AxonHubConnectionError(f"Invalid AxonHub base URL: {base_url}")

    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", "")
    )


class AxonHubClient:
    """Minimal AxonHub client for reading provider quota status."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        email: str,
        password: str,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._base_url = normalize_base_url(base_url)
        self._email = email
        self._password = password
        self._token: str | None = None
        self._token_obtained_at: datetime | None = None

    @property
    def base_url(self) -> str:
        """Return the normalized base URL."""
        return self._base_url

    @property
    def email(self) -> str:
        """Return the configured account email."""
        return self._email

    async def async_sign_in(self) -> None:
        """Authenticate against AxonHub and cache the returned JWT."""
        status, payload, raw, content_type = await self._async_request(
            "POST",
            SIGN_IN_PATH,
            json={"email": self._email, "password": self._password},
            timeout=REQUEST_TIMEOUT,
        )

        if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            raise AxonHubAuthError("AxonHub rejected the email or password")

        if status >= HTTPStatus.BAD_REQUEST:
            raise AxonHubApiError(
                f"AxonHub sign-in failed ({self._describe(status, payload, raw, content_type)})"
            )

        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise AxonHubApiError(
                "AxonHub sign-in response did not contain a token "
                f"({self._describe(status, payload, raw, content_type)})"
            )

        self._token = token
        self._token_obtained_at = dt_util.utcnow()
        _LOGGER.debug("Signed in to AxonHub at %s", self._base_url)

    def _token_is_usable(self) -> bool:
        """Return True while the cached JWT is still worth reusing."""
        if self._token is None or self._token_obtained_at is None:
            return False
        max_age = TOKEN_LIFETIME - TOKEN_REFRESH_MARGIN
        return dt_util.utcnow() - self._token_obtained_at < max_age

    async def async_graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        timeout: int = REQUEST_TIMEOUT,
        retry_on_auth: bool = True,
    ) -> dict[str, Any]:
        """Run a GraphQL operation against the admin endpoint."""
        if not self._token_is_usable():
            await self.async_sign_in()

        body: dict[str, Any] = {"query": query}
        if variables:
            body["variables"] = variables

        status, payload, raw, content_type = await self._async_request(
            "POST",
            GRAPHQL_PATH,
            json=body,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=timeout,
        )

        if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            if not retry_on_auth:
                raise AxonHubAuthError("AxonHub rejected the account credentials")
            # The JWT expired or the account was changed; sign in once more.
            _LOGGER.debug("AxonHub rejected the cached token, signing in again")
            await self.async_sign_in()
            return await self.async_graphql(
                query, variables, timeout=timeout, retry_on_auth=False
            )

        if status >= HTTPStatus.BAD_REQUEST:
            message = (
                "AxonHub rejected the GraphQL request "
                f"({self._describe(status, payload, raw, content_type)})"
            )
            _LOGGER.warning("%s — query sent: %s", message, _compact(query))
            raise AxonHubApiError(message)

        if not isinstance(payload, dict):
            message = (
                "AxonHub returned a response that is not GraphQL JSON "
                f"({self._describe(status, payload, raw, content_type)})"
            )
            _LOGGER.warning(
                "%s — check that the URL points at AxonHub itself and not at a "
                "reverse proxy error page",
                message,
            )
            raise AxonHubApiError(message)

        errors = payload.get("errors")
        if errors:
            message = _format_graphql_errors(errors)
            _LOGGER.warning(
                "AxonHub GraphQL error: %s — query sent: %s", message, _compact(query)
            )
            raise AxonHubApiError(message)

        data = payload.get("data")
        if not isinstance(data, dict):
            message = (
                "AxonHub GraphQL response did not contain data "
                f"({self._describe(status, payload, raw, content_type)})"
            )
            _LOGGER.warning("%s — query sent: %s", message, _compact(query))
            raise AxonHubApiError(message)

        return data

    async def async_get_channels(self) -> list[dict[str, Any]]:
        """Return every enabled channel together with its provider quota status."""
        data = await self.async_graphql(
            CHANNEL_QUOTA_QUERY,
            {"input": {"where": {"statusIn": ["enabled"]}}},
        )

        connection = data.get("queryChannels") or {}
        channels: list[dict[str, Any]] = []
        for edge in connection.get("edges") or []:
            node = (edge or {}).get("node")
            if isinstance(node, dict):
                channels.append(node)

        return channels

    async def async_trigger_quota_check(self) -> None:
        """Ask AxonHub to re-check every provider quota right now.

        AxonHub performs the check synchronously, so this call can take up to a
        few minutes on instances with many quota-enabled channels.
        """
        await self.async_graphql(TRIGGER_CHECK_MUTATION, timeout=CHECK_TIMEOUT)

    async def _async_request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout: int = REQUEST_TIMEOUT,
    ) -> tuple[int, Any, str, str]:
        """Perform a request and return ``(status, json, body, content_type)``."""
        url = f"{self._base_url}{path}"
        try:
            async with self._session.request(
                method,
                url,
                json=json,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                status = response.status
                content_type = response.headers.get("Content-Type", "")
                raw = await response.text()
        except asyncio.TimeoutError as err:
            raise AxonHubConnectionError(f"Timed out talking to {url}") from err
        except aiohttp.ClientError as err:
            raise AxonHubConnectionError(f"Error talking to {url}: {err}") from err

        payload: Any = None
        if raw:
            try:
                payload = json_module.loads(raw)
            except ValueError:
                payload = None

        if status >= HTTPStatus.BAD_REQUEST:
            _LOGGER.debug("AxonHub %s returned HTTP %s", path, status)

        return status, payload, raw, content_type

    @staticmethod
    def _describe(
        status: int, payload: Any, raw: str, content_type: str
    ) -> str:
        """Build a diagnosable description of an unexpected response."""
        detail = _extract_error_message(payload)
        if detail:
            return detail
        if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            return f"HTTP {status}"
        if raw:
            kind = f", {content_type}" if content_type else ""
            return f"HTTP {status}{kind}: {_compact(raw, _BODY_SNIPPET_LIMIT)}"
        return f"HTTP {status}"


def _extract_error_message(payload: Any) -> str | None:
    """Pull a human readable message out of an AxonHub error response."""
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if errors:
            return _format_graphql_errors(errors)
        for key in ("error", "message", "detail", "error_description"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(payload, list) and payload:
        return _format_graphql_errors(payload)
    return None


def _format_graphql_errors(errors: Any) -> str:
    """Turn a GraphQL error list into a readable message."""
    if isinstance(errors, dict):
        errors = [errors]
    if not isinstance(errors, list):
        return f"AxonHub returned an error: {errors}"

    messages = []
    for error in errors:
        if isinstance(error, dict):
            message = str(error.get("message") or error)
            path = error.get("path")
            if isinstance(path, list) and path:
                message = f"{message} (at {'/'.join(str(part) for part in path)})"
            messages.append(message)
        else:
            messages.append(str(error))

    return "; ".join(messages) or "AxonHub returned an unknown GraphQL error"


def _compact(text: str, limit: int = 120) -> str:
    """Collapse whitespace and truncate for log/error messages."""
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit]}…"
