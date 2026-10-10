"""End-to-end tests for the AxonHub HTTP client and quota parsing.

A mock AxonHub server is started on localhost for every test, so the real
``aiohttp`` based client is exercised: sign-in, the admin GraphQL request shape,
transparent re-authentication when the JWT is rejected, error mapping and the
normalization of each provider's ``quotaData`` payload.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
from custom_components.axonhub.stats import build_stats

EMAIL = "me@example.com"
PASSWORD = "secret"

# The `dashboardOverview` + `tokenStats` payload the integration reads.
# `averageResponseTime` is null upstream today, so the tests keep it null.
DASHBOARD_RESPONSE = {
    "dashboardOverview": {
        "totalRequests": 4210,
        "failedRequests": 37,
        "averageResponseTime": None,
        "requestStats": {
            "requestsToday": 128,
            "requestsThisWeek": 942,
            "requestsLastWeek": 1180,
            "requestsThisMonth": 3905,
        },
    },
    "tokenStats": {
        "totalInputTokensToday": 120000,
        "totalOutputTokensToday": 8000,
        "totalCachedTokensToday": 40000,
        "totalInputTokensThisWeek": 900000,
        "totalOutputTokensThisWeek": 60000,
        "totalCachedTokensThisWeek": 300000,
        "totalInputTokensThisMonth": 3000000,
        "totalOutputTokensThisMonth": 200000,
        "totalCachedTokensThisMonth": 1000000,
        "totalInputTokensAllTime": 12000000,
        "totalOutputTokensAllTime": 800000,
        "totalCachedTokensAllTime": 4000000,
        "lastUpdated": "2026-01-01T00:00:00Z",
    },
}

CLAUDE_CHANNEL = {
    "id": "gid://axonhub/channel/1",
    "name": "Claude Max",
    "type": "claudecode",
    "providerQuotaStatus": {
        "providerType": "claudecode",
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
        "providerType": "codex",
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
        "providerType": "github_copilot",
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
        "providerType": "nanogpt",
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
        "providerType": "claudecode",
        "status": "unknown",
        "ready": False,
        "nextResetAt": None,
        "nextCheckAt": "2026-01-01T00:20:00Z",
        "quotaData": {"error": "quota request failed: 401 unauthorized"},
    },
}

COMMANDCODE_CHANNEL = {
    "id": "gid://axonhub/channel/7",
    "name": "Command Code",
    "type": "commandcode",
    "providerQuotaStatus": {
        "providerType": "commandcode",
        "status": "available",
        "ready": True,
        "nextResetAt": "2026-02-01T00:00:00Z",
        "nextCheckAt": "2026-01-01T00:20:00Z",
        "quotaData": {
            "subscription_status": "active",
            "_limits": [
                {
                    "type": "subscription_cycle",
                    "status": "warning",
                    "usageRatio": 0.42,
                    "ready": True,
                    "window": "monthly",
                    "nextResetAt": "2026-02-01T00:00:00Z",
                    "periodStart": "2026-01-01T00:00:00Z",
                    "periodCost": 1.25,
                    "periodQuota": 5.0,
                },
                {
                    "type": "image",
                    "status": "available",
                    "usageRatio": 0.1,
                    "ready": True,
                    "window": "monthly",
                },
            ],
        },
    },
}

OPENCODE_CHANNEL = {
    "id": "gid://axonhub/channel/8",
    "name": "OpenCode Go",
    "type": "opencode_go_anthropic",
    "providerQuotaStatus": {
        "providerType": "opencode_go",
        "status": "available",
        "ready": True,
        "nextResetAt": None,
        "nextCheckAt": "2026-01-01T00:20:00Z",
        "quotaData": {
            "_limits": [
                {
                    "type": "token",
                    "status": "available",
                    "usageRatio": 0.72,
                    "ready": True,
                    "window": "weekly",
                }
            ]
        },
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
    COMMANDCODE_CHANNEL,
    OPENCODE_CHANNEL,
]


class MockAxonHub:
    """Stateful fake of the AxonHub auth + admin GraphQL endpoints."""

    def __init__(self) -> None:
        self.signins = 0
        self.checks = 0
        self.token = "jwt-0"
        self.graphql_bodies: list[dict[str, Any]] = []
        self.graphql_error: str | None = None
        self.partial_error: str | None = None

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

        if "dashboardOverview" in query:
            return web.json_response({"data": DASHBOARD_RESPONSE})

        if "systemGeneralSettings" in query:
            return web.json_response(
                {"data": {"systemGeneralSettings": {"currencyCode": "CNY"}}}
            )

        if "queryChannels" in query:
            channels = CHANNELS
            extra_errors: list[dict[str, Any]] = []
            if self.partial_error:
                # AxonHub nulls the field that failed to resolve and reports the
                # failure under `errors` with the path of the failing channel.
                channels = [dict(item) for item in CHANNELS]
                channels[2] = {**channels[2], "providerQuotaStatus": None}
                extra_errors = [
                    {
                        "message": self.partial_error,
                        "path": [
                            "queryChannels",
                            "edges",
                            2,
                            "node",
                            "providerQuotaStatus",
                        ],
                    }
                ]

            payload: dict[str, Any] = {
                "data": {
                    "queryChannels": {
                        "edges": [{"node": item} for item in channels],
                    }
                }
            }
            if extra_errors:
                payload["errors"] = extra_errors
            return web.json_response(payload)

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


async def test_query_requests_provider_type(mock_axonhub: Any) -> None:
    """The query must select ``providerType``.

    AxonHub's providerQuotaStatus resolver reads `pqs.ProviderType` while ent
    only loads selected columns, so omitting the field makes the resolver see
    the zero value and fail with `unsupported provider quota type: ""` for every
    channel. This is a subtle dependency that is easy to drop while editing the
    query, hence the explicit check.
    """
    import aiohttp

    state, base_url = mock_axonhub

    async with aiohttp.ClientSession() as session:
        client = AxonHubClient(session, base_url, EMAIL, PASSWORD)
        await client.async_get_channels()

    query = state.graphql_bodies[-1]["query"]
    assert "providerType" in query
    # accountKey only exists in newer AxonHub releases, so it must not be asked for.
    assert "accountKey" not in query


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


async def test_normalized_limits_are_used(mock_axonhub: Any) -> None:
    """Providers without a bespoke parser still get sensors from quotaData._limits."""
    import aiohttp

    _, base_url = mock_axonhub

    async with aiohttp.ClientSession() as session:
        client = AxonHubClient(session, base_url, EMAIL, PASSWORD)
        nodes = await client.async_get_channels()

    channel = next(
        channel
        for channel in (quota.build_channel_quota(node) for node in nodes)
        if channel is not None and channel.channel_key == "channel_7"
    )

    # Command Code has no raw-payload parser; only _limits feeds these metrics.
    assert sorted(metric.key for metric in channel.metrics) == [
        "limit_monthly_image",
        "limit_monthly_subscription_cycle",
    ]

    cycle = channel.metric("limit_monthly_subscription_cycle")
    assert cycle is not None
    assert cycle.value == 42.0
    assert cycle.unit == "%"
    # Same window twice, so names must be disambiguated by limit type.
    assert cycle.name == "Monthly subscription_cycle used"
    assert cycle.attributes["status"] == "warning"
    assert cycle.attributes["period_cost"] == 1.25
    assert cycle.attributes["period_quota"] == 5.0
    # The per-limit reset is a real timestamp rather than a raw string.
    assert cycle.reset_at is not None
    assert cycle.reset_at.isoformat() == "2026-02-01T00:00:00+00:00"
    # A bucket style window, so it names itself through the `limit` key and
    # keeps the provider's own identifier as the placeholder.
    assert cycle.is_rate_limit_window is False
    assert cycle.window_token == "Monthly subscription_cycle"

    image = channel.metric("limit_monthly_image")
    assert image is not None
    assert image.value == 10.0
    assert image.name == "Monthly image used"
    # No reset for this limit in the payload, so no reset sensor is built.
    assert image.reset_at is None


async def test_partial_graphql_errors_are_tolerated(mock_axonhub: Any) -> None:
    """One broken channel must not fail the whole poll.

    AxonHub returns `data` plus an `errors` entry when a single channel's quota
    resolver raises (for example an empty provider_type in the database).
    """
    import aiohttp

    state, base_url = mock_axonhub
    state.partial_error = (
        "failed to read provider quota collection settings: "
        'unsupported provider quota type: ""'
    )

    async with aiohttp.ClientSession() as session:
        client = AxonHubClient(session, base_url, EMAIL, PASSWORD)
        nodes = await client.async_get_channels()

    # Data still arrives: the affected channel simply has no quota status.
    assert len(nodes) == len(CHANNELS)
    assert nodes[0]["id"] == "gid://axonhub/channel/1"

    # The failure is attributed to the channel it belongs to (edge index 2).
    assert nodes[1].get("_quota_errors") is None
    assert nodes[2]["_quota_errors"] == [
        "failed to read provider quota collection settings: "
        'unsupported provider quota type: "" '
        "(at queryChannels/edges/2/node/providerQuotaStatus)"
    ]

    # And the parser reports it as that channel's error, with no metrics.
    broken = quota.build_channel_quota(nodes[2])
    assert broken is not None
    assert broken.status == "unknown"
    assert broken.metrics == []
    assert "unsupported provider quota type" in broken.error


async def test_channel_quota_parsing(mock_axonhub: Any) -> None:
    """Every provider payload is normalized into the expected metrics."""
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
        "channel_7",
        "channel_8",
    ]

    claude = channels["channel_1"]
    assert claude.provider == "claudecode"
    assert claude.status == "warning"
    assert claude.ready is True
    assert claude.next_reset_at is not None
    assert claude.next_reset_at.isoformat() == "2026-01-01T00:00:00+00:00"
    # The empty overage window is skipped.
    assert [metric.key for metric in claude.metrics] == ["window_5h", "window_7d"]
    assert claude.metrics[0].value == 82.0
    assert claude.metrics[0].unit == "%"
    # Each Claude window resets on its own: 5h and 7d differ.
    assert claude.metrics[0].reset_at is not None
    assert claude.metrics[0].reset_at.isoformat() == "2026-01-01T00:00:00+00:00"
    assert claude.metrics[1].reset_at is not None
    assert claude.metrics[1].reset_at.isoformat() == "2026-01-07T00:00:00+00:00"
    assert claude.metrics[0].attributes["reset"] == 1767225600

    codex = channels["channel_2"]
    assert [metric.key for metric in codex.metrics] == ["primary", "secondary"]
    assert codex.metrics[0].value == 43.5
    assert codex.metrics[0].attributes["limit_window_seconds"] == 18000
    assert codex.metrics[0].reset_at is not None
    assert codex.metrics[0].reset_at.isoformat() == "2026-01-01T00:00:00+00:00"
    # The secondary window carries no reset_at in the payload, so it gets none.
    assert codex.metrics[1].reset_at is None

    copilot = channels["channel_3"]
    assert sorted(metric.key for metric in copilot.metrics) == [
        "snapshot_completions",
        "snapshot_premium_interactions",
    ]
    premium = copilot.metric("snapshot_premium_interactions")
    assert premium is not None
    assert premium.value == 12.5
    assert premium.attributes["entitlement"] == 300.0
    # Copilot reports remaining percent; the payload here has no reset field.
    assert premium.reset_at is None
    completions = copilot.metric("snapshot_completions")
    assert completions is not None
    assert completions.value is None  # unlimited quota
    assert completions.attributes["unlimited"] is True

    nanogpt = channels["channel_4"]
    assert [metric.key for metric in nanogpt.metrics] == ["window_weeklyinputtokens"]
    assert nanogpt.metrics[0].value == 10.0
    # NanoGPT reports resetAt in milliseconds.
    assert nanogpt.metrics[0].reset_at is not None
    assert nanogpt.metrics[0].reset_at.isoformat() == "2026-01-01T00:00:00+00:00"
    assert nanogpt.metrics[0].attributes["resetAt"] == 1767225600000

    # providerType wins over the channel type when they differ.
    opencode = channels["channel_8"]
    assert opencode.channel_type == "opencode_go_anthropic"
    assert opencode.provider == "opencode_go"
    weekly = opencode.metric("limit_weekly_token")
    assert weekly is not None and weekly.value == 72.0

    # A failed provider check keeps the error but exposes no window sensors.
    broken = channels["channel_5"]
    assert broken.status == "unknown"
    assert broken.metrics == []
    assert broken.error == "quota request failed: 401 unauthorized"


async def test_dashboard_stats_query(mock_axonhub: Any) -> None:
    """The statistics client asks for both aggregates in one round trip."""
    import aiohttp

    state, base_url = mock_axonhub

    async with aiohttp.ClientSession() as session:
        client = AxonHubClient(session, base_url, EMAIL, PASSWORD)
        payload = await client.async_get_dashboard_stats()

    assert payload["dashboardOverview"]["totalRequests"] == 4210
    assert payload["tokenStats"]["totalInputTokensAllTime"] == 12000000

    body = state.graphql_bodies[-1]
    assert "dashboardOverview" in body["query"]
    assert "tokenStats" in body["query"]
    # No variables: both are root fields without arguments.
    assert "variables" not in body

    stats = build_stats(payload)
    assert stats is not None
    assert stats.requests.total == 4210
    assert stats.requests.failed == 37
    assert stats.requests.today == 128
    assert stats.requests.last_week == 1180
    assert stats.requests.success_rate == 99.12
    assert stats.tokens_today.total == 168000
    assert stats.tokens_today.cache_hit_rate == 25.0
    assert stats.tokens_all_time.total == 16800000


async def test_dashboard_stats_scope_error_is_an_api_error(
    mock_axonhub: Any,
) -> None:
    """A denied dashboard query surfaces the scope message to the caller."""
    import aiohttp

    state, base_url = mock_axonhub
    state.graphql_error = (
        "authz: principal user:1 does not have required scope read:dashboard"
    )

    async with aiohttp.ClientSession() as session:
        client = AxonHubClient(session, base_url, EMAIL, PASSWORD)
        with pytest.raises(AxonHubApiError) as err:
            await client.async_get_dashboard_stats()

    assert "read:dashboard" in str(err.value)


def test_build_stats_tolerates_partial_payloads() -> None:
    """Partial GraphQL answers degrade to zeros instead of raising."""
    # Neither field resolved: the caller must be able to tell "no statistics".
    assert build_stats({"dashboardOverview": None, "tokenStats": None}) is None
    assert build_stats({}) is None

    # Only the token aggregate resolved: requests fall back to zero, and an
    # all-zero window reports no cache hit rate rather than a bogus 0%.
    stats = build_stats(
        {"tokenStats": {"totalInputTokensToday": 10, "totalOutputTokensToday": 5}}
    )
    assert stats is not None
    assert stats.requests.total == 0
    assert stats.requests.success_rate is None
    assert stats.tokens_today.total == 15
    assert stats.tokens_today.cache_hit_rate == 0.0
    assert stats.tokens_this_week.total == 0
    assert stats.tokens_this_week.cache_hit_rate is None


def test_copilot_reset_epoch_unit_is_inferred() -> None:
    """Copilot's snapshot reset field is accepted in seconds and milliseconds.

    AxonHub copies ``quota_snapshots`` through verbatim and never reads
    ``quota_reset_at`` itself, so the integration infers the epoch unit from
    magnitude instead of assuming one and being wrong by a factor of 1000.
    """
    def reset_for(value: int) -> str | None:
        channel = quota.build_channel_quota(
            {
                "id": "gid://axonhub/channel/9",
                "name": "Copilot",
                "type": "github_copilot",
                "providerQuotaStatus": {
                    "providerType": "github_copilot",
                    "status": "available",
                    "ready": True,
                    "quotaData": {
                        "quota_snapshots": {
                            "premium_interactions": {
                                "percent_remaining": 12.5,
                                "quota_reset_at": value,
                            }
                        }
                    },
                },
            }
        )
        assert channel is not None
        metric = channel.metric("snapshot_premium_interactions")
        assert metric is not None
        return metric.reset_at.isoformat() if metric.reset_at else None

    assert reset_for(1767225600) == "2026-01-01T00:00:00+00:00"
    assert reset_for(1767225600000) == "2026-01-01T00:00:00+00:00"


def test_window_exposes_used_value_and_time_share() -> None:
    """A window reports the consumed side plus how much of it is left."""
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    metric = quota.QuotaMetric(
        key="window_5h",
        name="5h window used",
        value=82.0,
        unit="%",
        reset_at=start + timedelta(hours=5),
        period_start=start,
    )

    assert metric.window_label == "5h window"
    assert metric.used_value == 82.0
    # A rate limit window drops the "window" word from the placeholder and lets
    # the translation template supply it, so English stays "5h window used"
    # while Chinese can read "5h 窗口已用".
    assert metric.is_rate_limit_window is True
    assert metric.window_token == "5h"

    assert metric.time_remaining_percent(start) == 100.0
    assert metric.time_remaining_percent(start + timedelta(hours=2)) == 60.0
    assert metric.time_remaining_percent(start + timedelta(hours=5)) == 0.0
    # A poll landing after the stored reset reports nothing left rather than a
    # negative share, and without a window start there is no share at all.
    assert metric.time_remaining_percent(start + timedelta(hours=9)) == 0.0
    assert (
        quota.QuotaMetric(
            key="k", name="W used", value=1.0, reset_at=start + timedelta(hours=5)
        ).time_remaining_percent(start)
        is None
    )


def test_remaining_side_metric_reports_the_used_value() -> None:
    """Copilot reports remaining, so the entity value is inverted to used."""
    metric = quota.QuotaMetric(
        key="snapshot_premium_interactions",
        name="premium_interactions remaining",
        value=12.5,
        unit="%",
        used=False,
    )

    assert metric.window_label == "premium_interactions"
    assert metric.used_value == 87.5
    assert metric.is_rate_limit_window is False
    assert metric.window_token == "premium_interactions"


def test_no_window_no_percentage() -> None:
    """Without a usable value the consumed side stays ``None``, not 100."""
    metric = quota.QuotaMetric(key="k", name="W used", value=None)
    assert metric.used_value is None

    # ...and an unlimited Copilot snapshot does not become "0% used" either.
    unlimited = quota.QuotaMetric(
        key="snapshot_completions",
        name="completions remaining",
        value=None,
        used=False,
    )
    assert unlimited.used_value is None


async def test_currency_is_read_from_system_settings(mock_axonhub: Any) -> None:
    """The instance currency comes from systemGeneralSettings."""
    import aiohttp

    state, base_url = mock_axonhub

    async with aiohttp.ClientSession() as session:
        client = AxonHubClient(session, base_url, EMAIL, PASSWORD)
        assert await client.async_get_currency() == "CNY"

    body = state.graphql_bodies[-1]
    assert "systemGeneralSettings" in body["query"]
    assert "currencyCode" in body["query"]


async def test_currency_failure_is_tolerated(mock_axonhub: Any) -> None:
    """A failure to read the currency yields None instead of raising.

    It is only used to unit the money sensors, so a broken or older instance
    must not fail setup over it; the cost sensors are simply left out.
    """
    import aiohttp

    state, base_url = mock_axonhub
    state.graphql_error = "Cannot query field \"systemGeneralSettings\"."

    async with aiohttp.ClientSession() as session:
        client = AxonHubClient(session, base_url, EMAIL, PASSWORD)
        assert await client.async_get_currency() is None
