"""End-to-end tests for the AxonHub HTTP client and quota parsing.

A mock AxonHub server is started on localhost for every test, so the real
``aiohttp`` based client is exercised: sign-in, the admin GraphQL request shape,
transparent re-authentication when the JWT is rejected, error mapping and the
normalization of each provider's ``quotaData`` payload.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.axonhub import quota
from custom_components.axonhub.api import (
    AxonHubApiError,
    AxonHubAuthError,
    AxonHubClient,
    AxonHubConnectionError,
)

EMAIL = "me@example.com"
PASSWORD = "secret"

CLAUDE_CHANNEL = {
    "id": "gid://axonhub/channel/1",
    "name": "Claude Max",
    "type": "claudecode",
    "providerQuotaStatus": {
        "status": "warning",
        "ready": True,
        "nextResetAt": "2026-01-01T00:00:00Z",
        "nextCheckAt": "2026-01-01T00:20:00Z",
        "quotaData": {
            "unified_status": "allowed",
            "windows": {
                "5h": {"status": "allowed", "reset": 1767225600, "utilization": 0.82},
                "7d": {"status": "allowed", "reset": 1767744000, "utilization": 0.41},
                "overage": {"status": "", "reset": 0, "utilization": 0.0},
            },
            "representative_claim": "five_hour",
            "reset": 1767225600,
        },
    },
}

CODEX_CHANNEL = {
    "id": "gid://axonhub/channel/2",
    "name": "ChatGPT Plus",
    "type": "codex",
    "providerQuotaStatus": {
        "status": "available",
        "ready": True,
        "nextResetAt": None,
        "nextCheckAt": "2026-01-01T00:20:00Z",
        "quotaData": {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 43.5,
                    "reset_at": 1767225600,
                    "reset_after_seconds": 3600,
                    "limit_window_seconds": 18000,
                },
                "secondary_window": {
                    "used_percent": 12.0,
                    "limit_window_seconds": 604800,
                },
            },
        },
    },
}

COPILOT_CHANNEL = {
    "id": "gid://axonhub/channel/3",
    "name": "Copilot",
    "type": "github_copilot",
    "providerQuotaStatus": {
        "status": "warning",
        "ready": True,
        "nextResetAt": None,
        "nextCheckAt": "2026-01-01T00:20:00Z",
        "quotaData": {
            "quota_snapshots": {
                "premium_interactions": {
                    "entitlement": 300,
                    "has_quota": True,
                    "percent_remaining": 12.5,
                    "quota_id": "premium_interactions",
                    "quota_remaining": 37.0,
                    "unlimited": False,
                },
                "completions": {
                    "unlimited": True,
                    "percent_remaining": 100.0,
                    "quota_id": "completions",
                },
            }
        },
    },
}

NANOGPT_CHANNEL = {
    "id": "gid://axonhub/channel/4",
    "name": "NanoGPT",
    "type": "nanogpt",
    "providerQuotaStatus": {
        "status": "available",
        "ready": True,
        "nextResetAt": None,
        "nextCheckAt": "2026-01-01T00:20:00Z",
        "quotaData": {
            "windows": {
                "weeklyInputTokens": {
                    "used": 10,
                    "remaining": 90,
                    "percentUsed": 0.1,
                    "resetAt": 1767225600000,
                }
            }
        },
    },
}

BROKEN_CHANNEL = {
    "id": "gid://axonhub/channel/5",
    "name": "Broken",
    "type": "claudecode",
    "providerQuotaStatus": {
        "status": "unknown",
        "ready": False,
        "nextResetAt": None,
        "nextCheckAt": "2026-01-01T00:20:00Z",
        "quotaData": {"error": "quota request failed: 401 unauthorized"},
    },
}

UNSUPPORTED_CHANNEL = {
    "id": "gid://axonhub/channel/6",
    "name": "Plain Anthropic",
    "type": "anthropic",
    "providerQuotaStatus": None,
}

CHANNELS = [
    CLAUDE_CHANNEL,
    CODEX_CHANNEL,
    COPILOT_CHANNEL,
    NANOGPT_CHANNEL,
    BROKEN_CHANNEL,
    UNSUPPORTED_CHANNEL,
]


class MockAxonHub:
    """Stateful fake of the AxonHub auth + admin GraphQL endpoints."""

    def __init__(self) -> None:
        self.signins = 0
        self.checks = 0
        self.token = "jwt-0"
        self.graphql_bodies: list[dict[str, Any]] = []
        self.graphql_error: str | None = None

    async def sign_in(self, request: web.Request) -> web.Response:
        payload = await request.json()
        if payload.get("email") != EMAIL or payload.get("password") != PASSWORD:
            return web.json_response({"error": "Invalid email or password"}, status=401)

        self.signins += 1
        self.token = f"jwt-{self.signins}"
        return web.json_response(
            {"user": {"email": payload["email"]}, "token": self.token}
        )

    async def graphql(self, request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return web.json_response(
                {"errors": [{"message": "Invalid token"}]}, status=401
            )

        body = await request.json()
        self.graphql_bodies.append(body)
        query = body.get("query", "")

        if self.graphql_error:
            return web.json_response({"errors": [{"message": self.graphql_error}]})

        if "checkProviderQuotas" in query:
            self.checks += 1
            return web.json_response({"data": {"checkProviderQuotas": True}})

        if "queryChannels" in query:
            return web.json_response(
                {
                    "data": {
                        "queryChannels": {
                            "edges": [{"node": item} for item in CHANNELS]
                        }
                    }
                }
            )

        return web.json_response({"errors": [{"message": "unknown query"}]})


@pytest.fixture
async def mock_axonhub(socket_enabled: None) -> Any:
    """Run the mock AxonHub server and yield ``(state, base_url)``."""
    state = MockAxonHub()
    app = web.Application()
    app.router.add_post("/admin/auth/signin", state.sign_in)
    app.router.add_post("/admin/graphql", state.graphql)

    server = TestServer(app)
    await server.start_server()
    try:
        yield state, f"http://127.0.0.1:{server.port}"
    finally:
        await server.close()


async def test_sign_in_and_fetch_channels(mock_axonhub: Any) -> None:
    """The client signs in once and asks for enabled channels."""
    import aiohttp

    state, base_url = mock_axonhub

    async with aiohttp.ClientSession() as session:
        client = AxonHubClient(session, base_url, EMAIL, PASSWORD)
        nodes = await client.async_get_channels()

    assert len(nodes) == len(CHANNELS)
    assert state.signins == 1

    body = state.graphql_bodies[-1]
    assert body["variables"] == {"input": {"where": {"statusIn": ["enabled"]}}}
    assert "providerQuotaStatus" in body["query"]
    assert "quotaData" in body["query"]
    assert body["query"].lstrip().startswith("query")


async def test_base_url_is_normalized(mock_axonhub: Any) -> None:
    """A scheme-less URL gets an http:// prefix and no trailing slash."""
    import aiohttp

    _, base_url = mock_axonhub
    bare = base_url.removeprefix("http://")

    async with aiohttp.ClientSession() as session:
        client = AxonHubClient(session, f"{bare}/", EMAIL, PASSWORD)
        assert client.base_url == base_url


async def test_reauthenticates_when_token_is_rejected(mock_axonhub: Any) -> None:
    """A stale JWT triggers one transparent re-sign-in."""
    import aiohttp

    state, base_url = mock_axonhub

    async with aiohttp.ClientSession() as session:
        client = AxonHubClient(session, base_url, EMAIL, PASSWORD)
        await client.async_get_channels()
        assert state.signins == 1

        # Simulate an expired token that AxonHub still rejects.
        client._token = "stale-token"
        await client.async_get_channels()

    assert state.signins == 2


async def test_graphql_error_is_raised(mock_axonhub: Any) -> None:
    """GraphQL errors surface as AxonHubApiError with the server message."""
    import aiohttp

    state, base_url = mock_axonhub
    state.graphql_error = "permission denied: requires read:channels"

    async with aiohttp.ClientSession() as session:
        client = AxonHubClient(session, base_url, EMAIL, PASSWORD)
        with pytest.raises(AxonHubApiError, match="permission denied"):
            await client.async_get_channels()


async def test_invalid_credentials_raise_auth_error(mock_axonhub: Any) -> None:
    """A rejected sign-in raises AxonHubAuthError."""
    import aiohttp

    _, base_url = mock_axonhub

    async with aiohttp.ClientSession() as session:
        client = AxonHubClient(session, base_url, EMAIL, "wrong-password")
        with pytest.raises(AxonHubAuthError):
            await client.async_get_channels()


async def test_unsupported_field_is_reported_verbatim(mock_axonhub: Any) -> None:
    """A schema mismatch (old AxonHub) keeps the GraphQL message."""
    import aiohttp

    state, base_url = mock_axonhub
    state.graphql_error = 'Cannot query field "providerQuotaStatus" on type "Channel".'

    async with aiohttp.ClientSession() as session:
        client = AxonHubClient(session, base_url, EMAIL, PASSWORD)
        with pytest.raises(AxonHubApiError) as err:
            await client.async_get_channels()

    assert "Cannot query field" in str(err.value)


async def test_html_response_is_reported_with_status_and_body(
    socket_enabled: None,
) -> None:
    """A non-JSON answer (reverse proxy page) is quoted in the error."""
    import aiohttp

    async def html_handler(request: web.Request) -> web.Response:
        return web.Response(
            text="<html><body>502 Bad Gateway</body></html>",
            content_type="text/html",
            status=502,
        )

    app = web.Application()
    app.router.add_post("/admin/auth/signin", html_handler)

    server = TestServer(app)
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as session:
            client = AxonHubClient(
                session, f"http://127.0.0.1:{server.port}", EMAIL, PASSWORD
            )
            with pytest.raises(AxonHubApiError) as err:
                await client.async_sign_in()
    finally:
        await server.close()

    message = str(err.value)
    assert "502" in message
    assert "Bad Gateway" in message


async def test_unreachable_host_raises_connection_error(socket_enabled: None) -> None:
    """A dead endpoint raises AxonHubConnectionError, not a raw aiohttp error."""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        client = AxonHubClient(session, "http://127.0.0.1:1", EMAIL, PASSWORD)
        with pytest.raises(AxonHubConnectionError):
            await client.async_get_channels()


async def test_trigger_quota_check(mock_axonhub: Any) -> None:
    """The refresh service calls the checkProviderQuotas mutation."""
    import aiohttp

    state, base_url = mock_axonhub

    async with aiohttp.ClientSession() as session:
        client = AxonHubClient(session, base_url, EMAIL, PASSWORD)
        await client.async_trigger_quota_check()

    assert state.checks == 1
    assert "checkProviderQuotas" in state.graphql_bodies[-1]["query"]


async def test_channel_quota_parsing(mock_axonhub: Any) -> None:
    """Every provider payload is normalized into the expected metrics."""
    import aiohttp

    _, base_url = mock_axonhub

    async with aiohttp.ClientSession() as session:
        client = AxonHubClient(session, base_url, EMAIL, PASSWORD)
        nodes = await client.async_get_channels()

    channels = {
        channel.channel_key: channel
        for channel in (quota.build_channel_quota(node) for node in nodes)
        if channel is not None
    }

    # Channels without a quota checker are dropped.
    assert sorted(channels) == [
        "channel_1",
        "channel_2",
        "channel_3",
        "channel_4",
        "channel_5",
    ]

    claude = channels["channel_1"]
    assert claude.status == "warning"
    assert claude.ready is True
    assert claude.next_reset_at is not None
    assert claude.next_reset_at.isoformat() == "2026-01-01T00:00:00+00:00"
    # The empty overage window is skipped.
    assert [metric.key for metric in claude.metrics] == ["window_5h", "window_7d"]
    assert claude.metrics[0].value == 82.0
    assert claude.metrics[0].unit == "%"
    assert claude.metrics[0].attributes["reset_at"].startswith("2026-01-01")

    codex = channels["channel_2"]
    assert [metric.key for metric in codex.metrics] == ["primary", "secondary"]
    assert codex.metrics[0].value == 43.5
    assert codex.metrics[0].attributes["limit_window_seconds"] == 18000

    copilot = channels["channel_3"]
    assert sorted(metric.key for metric in copilot.metrics) == [
        "snapshot_completions",
        "snapshot_premium_interactions",
    ]
    premium = copilot.metric("snapshot_premium_interactions")
    assert premium is not None
    assert premium.value == 12.5
    assert premium.attributes["entitlement"] == 300.0
    completions = copilot.metric("snapshot_completions")
    assert completions is not None
    assert completions.value is None  # unlimited quota
    assert completions.attributes["unlimited"] is True

    nanogpt = channels["channel_4"]
    assert [metric.key for metric in nanogpt.metrics] == ["window_weeklyinputtokens"]
    assert nanogpt.metrics[0].value == 10.0
    assert nanogpt.metrics[0].attributes["reset_at"].startswith("2026-01-01")

    # A failed provider check keeps the error but exposes no window sensors.
    broken = channels["channel_5"]
    assert broken.status == "unknown"
    assert broken.metrics == []
    assert broken.error == "quota request failed: 401 unauthorized"
