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
#
# `providerType` is REQUIRED, not decorative: AxonHub's `providerQuotaStatus`
# resolver reads `pqs.ProviderType` to decide whether quota collection is
# enabled for that provider, and ent only loads the columns that were actually
# selected. Without this field the resolver sees the zero value and fails with
# `unsupported provider quota type: ""`, which nulls the field for every
# channel. The web UI requests it for the same reason.
#
# `accountKey` is deliberately NOT requested: it was added in a later release
# than `providerQuotaStatus` itself (AxonHub PR #2381), so asking for it would
# break older instances.
CHANNEL_QUOTA_QUERY = """
query HomeAssistantProviderQuotas($input: QueryChannelInput!) {
  queryChannels(input: $input) {
    edges {
      node {
        id
        name
        type
        providerQuotaStatus {
          providerType
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

# Instance wide aggregates for the hub device: request counters and token sums.
#
# `dashboardOverview.requestStats` counts `usage_logs` rows (successful results,
# the numbers the AxonHub dashboard charts) while `totalRequests` /
# `failedRequests` count rows of the `requests` table (process tracking, so
# failures are included). Both are requested so the difference stays visible in
# the entities instead of being silently reconciled here.
#
# `tokenStats` covers today/week/month/all-time in one field; the all-time part
# is served from an upstream stale-while-revalidate cache, so polling it on the
# regular scan interval does not add a table scan every time.
#
# `averageResponseTime` is nullable upstream (AxonHub does not compute it yet);
# it is requested anyway so it starts working without another integration
# release once AxonHub fills it in.
DASHBOARD_STATS_QUERY = """
query HomeAssistantDashboardStats {
  dashboardOverview {
    totalRequests
    failedRequests
    averageResponseTime
    requestStats {
      requestsToday
      requestsThisWeek
      requestsLastWeek
      requestsThisMonth
    }
  }
  tokenStats {
    totalInputTokensToday
    totalOutputTokensToday
    totalCachedTokensToday
    totalInputTokensThisWeek
    totalOutputTokensThisWeek
    totalCachedTokensThisWeek
    totalInputTokensThisMonth
    totalOutputTokensThisMonth
    totalCachedTokensThisMonth
    totalInputTokensAllTime
    totalOutputTokensAllTime
    totalCachedTokensAllTime
    lastUpdated
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
        data, errors = await self.async_graphql_with_errors(
            query, variables, timeout=timeout, retry_on_auth=retry_on_auth
        )
        if errors:
            raise AxonHubApiError(_format_graphql_errors(errors))
        return data

    async def async_graphql_with_errors(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        timeout: int = REQUEST_TIMEOUT,
        retry_on_auth: bool = True,
    ) -> tuple[dict[str, Any], list[Any]]:
        """Run a GraphQL operation, returning ``(data, errors)``.

        GraphQL answers with partial ``data`` plus an ``errors`` list when a
        single field fails. AxonHub does this, for example, for a channel whose
        stored provider quota row has an empty `provider_type`: that channel's
        ``providerQuotaStatus`` resolves to null while every other channel still
        works. Both parts are returned so callers can keep the usable data and
        report the failure per channel instead of dropping the whole response.
        """
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
            return await self.async_graphql_with_errors(
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

        errors = payload.get("errors") or []
        data = payload.get("data")

        if errors and not (isinstance(data, dict) and data):
            message = _format_graphql_errors(errors)
            _LOGGER.warning(
                "AxonHub GraphQL error: %s — query sent: %s", message, _compact(query)
            )
            raise AxonHubApiError(message)

        if not isinstance(data, dict):
            message = (
                "AxonHub GraphQL response did not contain data "
                f"({self._describe(status, payload, raw, content_type)})"
            )
            _LOGGER.warning("%s — query sent: %s", message, _compact(query))
            raise AxonHubApiError(message)

        return data, errors

    async def async_get_channels(self) -> list[dict[str, Any]]:
        """Return every enabled channel together with its provider quota status.

        Channels whose ``providerQuotaStatus`` failed to resolve keep their
        place in the list and carry the server side reason under
        ``_quota_errors`` so the affected sensor can report it.
        """
        data, errors = await self.async_graphql_with_errors(
            CHANNEL_QUOTA_QUERY,
            {"input": {"where": {"statusIn": ["enabled"]}}},
        )

        connection = data.get("queryChannels") or {}
        channels: list[dict[str, Any]] = []
        edge_indexes: list[int] = []
        for index, edge in enumerate(connection.get("edges") or []):
            node = (edge or {}).get("node")
            if isinstance(node, dict):
                channels.append(node)
                edge_indexes.append(index)

        if errors:
            _attach_quota_errors(channels, edge_indexes, errors)

        return channels

    async def async_get_dashboard_stats(self) -> dict[str, Any]:
        """Return AxonHub's instance wide dashboard aggregates.

        Raises :class:`AxonHubApiError` when AxonHub refuses both root fields —
        typically an account without the ``read:dashboard`` scope, or an older
        build that predates the dashboard API. GraphQL allows a partial answer,
        so a single failing field still returns the other one.
        """
        data, errors = await self.async_graphql_with_errors(DASHBOARD_STATS_QUERY)

        if errors:
            _LOGGER.debug(
                "AxonHub answered the dashboard statistics partially: %s",
                _format_graphql_errors(errors),
            )

        return data

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


def _quota_status_path_index(error: Any) -> int | None:
    """Extract the edge index from a ``queryChannels`` error path.

    AxonHub reports a failing channel as
    ``["queryChannels", "edges", 12, "node", "providerQuotaStatus"]``.
    """
    if not isinstance(error, dict):
        return None

    path = error.get("path")
    if not isinstance(path, list) or len(path) < 3:
        return None
    if path[0] != "queryChannels" or path[1] != "edges":
        return None

    index = path[2]
    if isinstance(index, bool) or not isinstance(index, int):
        return None
    return index


def _attach_quota_errors(
    channels: list[dict[str, Any]], edge_indexes: list[int], errors: list[Any]
) -> None:
    """Attach each ``providerQuotaStatus`` failure to the channel it belongs to."""
    unattributed: list[str] = []

    for error in errors:
        index = _quota_status_path_index(error)
        message = _format_graphql_errors([error])

        if index is not None and index in edge_indexes:
            node = channels[edge_indexes.index(index)]
            node.setdefault("_quota_errors", []).append(message)
            _LOGGER.warning(
                "AxonHub could not resolve the provider quota of channel %s: %s",
                node.get("name") or node.get("id"),
                message,
            )
            continue

        unattributed.append(message)

    if unattributed:
        _LOGGER.warning(
            "AxonHub reported errors that are not tied to a channel, continuing "
            "without them: %s",
            "; ".join(unattributed),
        )


def _compact(text: str, limit: int = 120) -> str:
    """Collapse whitespace and truncate for log/error messages."""
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit]}…"
