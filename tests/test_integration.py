"""Tests for the AxonHub custom integration.

The AxonHub HTTP layer is exercised against a real mock server in
``test_api.py``; here the client methods are patched so the tests focus on the
Home Assistant plumbing: config flow, coordinator, entity/device creation,
services and re-authentication.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.axonhub.api import (
    AxonHubApiError,
    AxonHubAuthError,
    AxonHubClient,
)
from custom_components.axonhub.const import (
    CONF_BASE_URL,
    CONF_VERIFY_SSL,
    DOMAIN,
    MIN_AXONHUB_VERSION,
    SERVICE_REFRESH_QUOTAS,
)

BASE_URL = "http://axon.local:8090"

CLAUDE_DATA = {
    "unified_status": "allowed",
    "windows": {
        "5h": {"status": "allowed", "reset": 1767225600, "utilization": 0.82},
        "7d": {"status": "allowed", "reset": 1767744000, "utilization": 0.41},
        "overage": {"status": "", "reset": 0, "utilization": 0.0},
    },
    "representative_claim": "five_hour",
    "reset": 1767225600,
}

CODEX_DATA = {
    "plan_type": "plus",
    "rate_limit": {
        "allowed": True,
        "limit_reached": False,
        "primary_window": {
            "used_percent": 43.5,
            "reset_at": 1767225600,
            "reset_after_seconds": 3600,
            "limit_window_seconds": 18000,
        },
        "secondary_window": {
            "used_percent": 12.0,
            "reset_at": 1767744000,
            "reset_after_seconds": 90000,
            "limit_window_seconds": 604800,
        },
    },
}

COPILOT_DATA = {
    "plan_type": "individual",
    "quota_snapshots": {
        "premium_interactions": {
            "entitlement": 300,
            "has_quota": True,
            "percent_remaining": 12.5,
            "quota_id": "premium_interactions",
            "quota_remaining": 37.0,
            "quota_reset_at": 1767225600,
            "unlimited": False,
        },
    },
}


def node(
    channel_id: int,
    name: str,
    channel_type: str,
    status: str | None = "available",
    data: dict[str, Any] | None = None,
    ready: bool = True,
    next_reset: str | None = "2026-01-01T00:00:00Z",
) -> dict[str, Any]:
    """Build a ``queryChannels`` node the way AxonHub returns it."""
    return {
        "id": f"gid://axonhub/channel/{channel_id}",
        "name": name,
        "type": channel_type,
        "providerQuotaStatus": None
        if status is None
        else {
            "providerType": channel_type,
            "status": status,
            "ready": ready,
            "nextResetAt": next_reset,
            "nextCheckAt": "2026-01-01T00:20:00Z",
            "quotaData": data or {},
        },
    }


CHANNELS = [
    node(1, "Claude Max", "claudecode", "warning", CLAUDE_DATA),
    node(2, "ChatGPT Plus", "codex", "available", CODEX_DATA),
    node(3, "Copilot", "github_copilot", "warning", COPILOT_DATA),
    node(4, "Plain Anthropic", "anthropic", None),
]

# Shape of the `dashboardOverview` + `tokenStats` response, trimmed to the
# fields the integration reads.
DASHBOARD_DATA: dict[str, Any] = {
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


class PatchedClient:
    """Records which client methods the integration called."""

    def __init__(self) -> None:
        self.signins = 0
        self.channel_reads = 0
        self.stats_reads = 0
        self.checks = 0
        self.currency_reads = 0
        self.fail_auth = False
        self.api_error: str | None = None
        self.stats_error: str | None = None
        self.stats: dict[str, Any] = DASHBOARD_DATA
        self.currency: str | None = "USD"
        self.channels: list[dict[str, Any]] = CHANNELS


@pytest.fixture
def patched_client(monkeypatch: pytest.MonkeyPatch) -> PatchedClient:
    """Patch the AxonHub HTTP client so no network is involved."""
    recorder = PatchedClient()

    async def _sign_in(self) -> None:
        recorder.signins += 1
        if recorder.fail_auth:
            raise AxonHubAuthError("Invalid email or password")
        if recorder.api_error:
            raise AxonHubApiError(recorder.api_error)

    async def _get_channels(self) -> list[dict[str, Any]]:
        recorder.channel_reads += 1
        if recorder.fail_auth:
            raise AxonHubAuthError("Invalid email or password")
        if recorder.api_error:
            raise AxonHubApiError(recorder.api_error)
        return recorder.channels

    async def _get_dashboard_stats(self) -> dict[str, Any]:
        recorder.stats_reads += 1
        if recorder.fail_auth:
            raise AxonHubAuthError("Invalid email or password")
        if recorder.stats_error:
            raise AxonHubApiError(recorder.stats_error)
        return recorder.stats

    async def _trigger(self) -> None:
        recorder.checks += 1

    async def _get_currency(self) -> str | None:
        recorder.currency_reads += 1
        return recorder.currency

    monkeypatch.setattr(AxonHubClient, "async_sign_in", _sign_in)
    monkeypatch.setattr(AxonHubClient, "async_get_channels", _get_channels)
    monkeypatch.setattr(
        AxonHubClient, "async_get_dashboard_stats", _get_dashboard_stats
    )
    monkeypatch.setattr(AxonHubClient, "async_trigger_quota_check", _trigger)
    monkeypatch.setattr(AxonHubClient, "async_get_currency", _get_currency)
    return recorder


def make_entry(**overrides: Any) -> MockConfigEntry:
    """Build a config entry pointing at the fake AxonHub."""
    data = {
        CONF_BASE_URL: BASE_URL,
        CONF_EMAIL: "me@example.com",
        CONF_PASSWORD: "secret",
        CONF_VERIFY_SSL: True,
    }
    data.update(overrides)
    return MockConfigEntry(
        domain=DOMAIN,
        title="axon.local:8090",
        data=data,
        unique_id=BASE_URL,
    )


async def test_config_flow_creates_entry(
    hass: HomeAssistant, patched_client: PatchedClient
) -> None:
    """The user flow validates the credentials and creates an entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_BASE_URL: BASE_URL,
            CONF_EMAIL: "me@example.com",
            CONF_PASSWORD: "secret",
            CONF_VERIFY_SSL: True,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert result["title"] == "axon.local:8090"
    assert result["data"][CONF_BASE_URL] == BASE_URL
    # One sign-in for the credential check; the client caches its JWT for the
    # quota and statistics reads that follow.
    assert patched_client.signins >= 1
    # Validation reads the channels once; Home Assistant then sets the freshly
    # created entry up, which reads them again.
    assert patched_client.channel_reads >= 1
    assert patched_client.stats_reads >= 1


async def test_config_flow_rejects_bad_credentials(
    hass: HomeAssistant, patched_client: PatchedClient
) -> None:
    """A rejected sign-in re-renders the form with an error."""
    patched_client.fail_auth = True

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_BASE_URL: BASE_URL,
            CONF_EMAIL: "me@example.com",
            CONF_PASSWORD: "wrong",
            CONF_VERIFY_SSL: True,
        },
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_config_flow_reports_graphql_error(
    hass: HomeAssistant, patched_client: PatchedClient
) -> None:
    """A GraphQL failure shows the real AxonHub message instead of a bare error."""
    patched_client.api_error = "AxonHub GraphQL error: boom"

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_BASE_URL: BASE_URL,
            CONF_EMAIL: "me@example.com",
            CONF_PASSWORD: "secret",
            CONF_VERIFY_SSL: True,
        },
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "api_error"}
    assert result["description_placeholders"]["error"] == "AxonHub GraphQL error: boom"


async def test_config_flow_detects_unsupported_axonhub(
    hass: HomeAssistant, patched_client: PatchedClient
) -> None:
    """An AxonHub without the quota API is reported as an upgrade problem."""
    patched_client.api_error = (
        'Cannot query field "providerQuotaStatus" on type "Channel".'
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_BASE_URL: BASE_URL,
            CONF_EMAIL: "me@example.com",
            CONF_PASSWORD: "secret",
            CONF_VERIFY_SSL: True,
        },
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "unsupported_version"}
    assert result["description_placeholders"]["minimum_version"] == MIN_AXONHUB_VERSION


async def test_setup_creates_quota_entities(
    hass: HomeAssistant, patched_client: PatchedClient
) -> None:
    """Setting up the entry creates one device per quota-enabled channel."""
    entry = make_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    # Claude Code channel: status, reset timestamp and one sensor per window.
    status = hass.states.get("sensor.claude_max_quota_status")
    assert status is not None, sorted(hass.states.async_entity_ids())
    assert status.state == "warning"
    assert status.attributes["provider"] == "claudecode"
    assert status.attributes["ready"] is True
    assert status.attributes["channel_id"] == "gid://axonhub/channel/1"
    assert status.attributes["quota_data"]["unified_status"] == "allowed"
    assert status.attributes["next_reset_at"] == "2026-01-01T00:00:00+00:00"

    five_hour = hass.states.get("sensor.claude_max_5h_window_used")
    assert five_hour is not None
    assert five_hour.state == "82.0"
    assert five_hour.attributes["unit_of_measurement"] == "%"
    assert five_hour.attributes["next_reset_at"].startswith("2026-01-01")

    seven_day = hass.states.get("sensor.claude_max_7d_window_used")
    assert seven_day is not None
    assert seven_day.state == "41.0"

    # The empty "overage" window must not create an entity.
    assert hass.states.get("sensor.claude_max_overage_window_used") is None

    # Every window carries its own reset timestamp; the two Claude windows
    # reset at different times.
    five_hour_reset = hass.states.get("sensor.claude_max_5h_window_reset")
    assert five_hour_reset is not None
    assert five_hour_reset.state == "2026-01-01T00:00:00+00:00"
    assert five_hour_reset.attributes["device_class"] == "timestamp"
    # The paired percentage rides along so one card row can show both.
    assert five_hour_reset.attributes["value"] == 82.0
    assert five_hour_reset.attributes["unit"] == "%"

    seven_day_reset = hass.states.get("sensor.claude_max_7d_window_reset")
    assert seven_day_reset is not None
    assert seven_day_reset.state == "2026-01-07T00:00:00+00:00"

    # The channel wide reset entity is gone: reset is per window now.
    assert hass.states.get("sensor.claude_max_next_quota_reset") is None

    # Windows expose the consumed side only; the remaining entity is gone.
    assert hass.states.get("sensor.claude_max_5h_window_remaining") is None
    assert hass.states.get("sensor.claude_max_7d_window_remaining") is None

    five_hour_time = hass.states.get("sensor.claude_max_5h_window_time_elapsed")
    assert five_hour_time is not None
    assert five_hour_time.attributes["unit_of_measurement"] == "%"
    # The fixture resets in the past relative to the test clock, so the whole
    # window counts as elapsed and the share clamps to 100 rather than
    # overshooting.
    assert five_hour_time.state == "100.0"
    assert five_hour_time.attributes["period_start"] == "2025-12-31T19:00:00+00:00"
    assert five_hour_time.attributes["reset_at"] == "2026-01-01T00:00:00+00:00"

    ready = hass.states.get("binary_sensor.claude_max_quota_ready")
    assert ready is not None
    assert ready.state == "on"

    # Codex channel.
    assert hass.states.get("sensor.chatgpt_plus_primary_window_used").state == "43.5"
    assert hass.states.get("sensor.chatgpt_plus_secondary_window_used").state == "12.0"
    assert (
        hass.states.get("sensor.chatgpt_plus_primary_window_reset").state
        == "2026-01-01T00:00:00+00:00"
    )
    assert (
        hass.states.get("sensor.chatgpt_plus_secondary_window_reset").state
        == "2026-01-07T00:00:00+00:00"
    )

    # Copilot channel. Its snapshot reports *remaining* percent (12.5), so the
    # entity is inverted to the consumed side and carries the `_used` id; the
    # remaining entity is gone like everywhere else.
    premium = hass.states.get("sensor.copilot_premium_interactions_used")
    assert premium is not None
    assert premium.state == "87.5"
    assert premium.attributes["entitlement"] == 300.0
    assert (
        hass.states.get("sensor.copilot_premium_interactions_reset").state
        == "2026-01-01T00:00:00+00:00"
    )
    assert hass.states.get("sensor.copilot_premium_interactions_remaining") is None

    # Channels without a quota checker are ignored entirely.
    assert not [
        entity_id
        for entity_id in hass.states.async_entity_ids()
        if "plain_anthropic" in entity_id
    ]

    # Look the devices up per config entry: HA 2026.10 deprecated both mapping
    # access to `DeviceRegistry.devices` and `DeviceEntry.config_entries`, while
    # iterating the registry directly only yields entries from that release on.
    # `async_entries_for_config_entry` behaves the same across the supported HA
    # range, and filtering by entry id already proves ownership, so no
    # `config_entries` assertion is needed.
    devices = dr.async_get(hass)
    channel_devices = [
        device
        for device in dr.async_entries_for_config_entry(devices, entry.entry_id)
        if device.name in {"Claude Max", "ChatGPT Plus", "Copilot"}
    ]
    assert len(channel_devices) == 3, [device.name for device in channel_devices]

    hub = next(
        device
        for device in dr.async_entries_for_config_entry(devices, entry.entry_id)
        if (DOMAIN, entry.entry_id) in device.identifiers
    )
    assert hub.name == "axon.local:8090"
    assert hub.configuration_url == BASE_URL


async def test_legacy_next_reset_entity_is_removed(
    hass: HomeAssistant, patched_client: PatchedClient
) -> None:
    """The channel wide reset entity of earlier releases is cleaned up.

    It has no replacement with the same unique_id (resets are per window now),
    so without this it would stay in the registry as an unavailable leftover.
    """
    entry = make_entry()
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_channel_1_next_reset",
        config_entry=entry,
        suggested_object_id="claude_max_next_quota_reset",
    )
    assert registry.async_get("sensor.claude_max_next_quota_reset") is not None

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get("sensor.claude_max_next_quota_reset") is None
    # The per window entities took its place.
    assert registry.async_get("sensor.claude_max_5h_window_reset") is not None


async def test_legacy_remaining_entities_are_removed(
    hass: HomeAssistant, patched_client: PatchedClient
) -> None:
    """The quota-remaining entities of earlier releases are cleaned up.

    A derived one (Claude) and Copilot's primary one are registered on purpose:
    the latter's unique id carries no ``remaining`` suffix, so cleanup has to
    match the entity id too. ``_time_remaining`` is a leftover as well now that
    the window time entity reports the elapsed side under ``_time_elapsed``.
    """
    entry = make_entry()
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    for unique_id, object_id in (
        # The derived counterpart Claude Code used to get.
        (f"{entry.entry_id}_channel_1_window_5h_remaining", "claude_max_5h_window_remaining"),
        # Copilot's primary entity, which was the remaining side.
        (
            f"{entry.entry_id}_channel_3_snapshot_premium_interactions",
            "copilot_premium_interactions_remaining",
        ),
        # Also a leftover: the elapsed side replaced it.
        (
            f"{entry.entry_id}_channel_1_window_5h_time_remaining",
            "claude_max_5h_window_time_remaining",
        ),
    ):
        registry.async_get_or_create(
            "sensor", DOMAIN, unique_id, config_entry=entry, suggested_object_id=object_id
        )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get("sensor.claude_max_5h_window_remaining") is None
    assert registry.async_get("sensor.copilot_premium_interactions_remaining") is None
    assert registry.async_get("sensor.claude_max_5h_window_time_remaining") is None
    # ...and the elapsed side took its place.
    assert registry.async_get("sensor.claude_max_5h_window_time_elapsed") is not None
    # Copilot's percentage entity is now the consumed side.
    assert registry.async_get("sensor.copilot_premium_interactions_used") is not None


async def test_setup_creates_instance_statistics(
    hass: HomeAssistant, patched_client: PatchedClient
) -> None:
    """The hub device carries the request and token counters of the instance."""
    entry = make_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    total = hass.states.get("sensor.axon_local_8090_total_requests")
    assert total is not None, sorted(hass.states.async_entity_ids())
    assert total.state == "4210"
    assert total.attributes["unit_of_measurement"] == "requests"
    assert total.attributes["failed_requests"] == 37
    assert total.attributes["success_rate"] == 99.12
    # AxonHub does not compute the average yet, so it must stay absent instead
    # of showing up as None.
    assert "average_response_time" not in total.attributes

    assert hass.states.get("sensor.axon_local_8090_failed_requests").state == "37"
    assert hass.states.get("sensor.axon_local_8090_requests_today").state == "128"
    assert hass.states.get("sensor.axon_local_8090_requests_this_week").state == "942"
    assert hass.states.get("sensor.axon_local_8090_requests_last_week").state == "1180"
    assert hass.states.get("sensor.axon_local_8090_requests_this_month").state == "3905"

    # Cached input tokens are excluded from prompt_tokens upstream, so the total
    # adds them back: 120000 + 8000 + 40000.
    today = hass.states.get("sensor.axon_local_8090_tokens_today")
    assert today is not None
    assert today.state == "168000"
    assert today.attributes["unit_of_measurement"] == "tokens"
    assert today.attributes["input_tokens"] == 120000
    assert today.attributes["output_tokens"] == 8000
    assert today.attributes["cached_tokens"] == 40000
    assert today.attributes["cache_hit_rate"] == 25.0

    assert hass.states.get("sensor.axon_local_8090_tokens_this_week").state == "1260000"
    assert (
        hass.states.get("sensor.axon_local_8090_tokens_this_month").state == "4200000"
    )
    assert hass.states.get("sensor.axon_local_8090_tokens_all_time").state == "16800000"

    available = hass.states.get("binary_sensor.axon_local_8090_statistics_available")
    assert available is not None
    assert available.state == "on"

    # Every instance entity belongs to the hub device rather than a new one.
    devices = dr.async_get(hass)
    hub = next(
        device
        for device in dr.async_entries_for_config_entry(devices, entry.entry_id)
        if (DOMAIN, entry.entry_id) in device.identifiers
    )
    registry = er.async_get(hass)
    hub_entities = {
        registry.async_get(entity_id).device_id
        for entity_id in (
            "sensor.axon_local_8090_total_requests",
            "sensor.axon_local_8090_tokens_today",
            "binary_sensor.axon_local_8090_statistics_available",
        )
    }
    assert hub_entities == {hub.id}


async def test_statistics_failure_keeps_quota_entities(
    hass: HomeAssistant, patched_client: PatchedClient
) -> None:
    """An account without the dashboard scope only loses the instance sensors."""
    patched_client.stats_error = (
        "authz: principal user:1 does not have required scope read:dashboard"
    )

    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    # Quota entities keep working...
    assert hass.states.get("sensor.claude_max_5h_window_used").state == "82.0"
    # ...while the instance statistics report themselves as unavailable.
    assert (
        hass.states.get("binary_sensor.axon_local_8090_statistics_available").state
        == "off"
    )
    for entity_id in (
        "sensor.axon_local_8090_total_requests",
        "sensor.axon_local_8090_tokens_today",
    ):
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "unavailable"


async def test_refresh_quotas_service(
    hass: HomeAssistant, patched_client: PatchedClient
) -> None:
    """The custom service triggers a provider check on AxonHub."""
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.services.has_service(DOMAIN, SERVICE_REFRESH_QUOTAS)

    await hass.services.async_call(DOMAIN, SERVICE_REFRESH_QUOTAS, {}, blocking=True)
    await hass.async_block_till_done()
    assert patched_client.checks == 1

    await hass.services.async_call(
        DOMAIN,
        SERVICE_REFRESH_QUOTAS,
        {"entry_id": entry.entry_id},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert patched_client.checks == 2


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, patched_client: PatchedClient
) -> None:
    """Rejected credentials put the entry in setup error and start reauth."""
    patched_client.fail_auth = True

    entry = make_entry()
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"]["source"] == "reauth" for flow in flows), flows


# A channel whose payload uses the normalized `_limits` shape, which is the one
# that carries the money AxonHub prices from its usage logs.
PRICED_DATA = {
    "subscription_status": "active",
    "_limits": [
        {
            "type": "token",
            "status": "warning",
            "usageRatio": 0.42,
            "ready": True,
            "window": "monthly",
            "nextResetAt": "2026-02-01T00:00:00Z",
            "periodStart": "2026-01-01T00:00:00Z",
            "periodCost": 1.25,
            "periodQuota": 5.0,
        }
    ],
}


async def test_cost_entity_uses_instance_currency(
    hass: HomeAssistant, patched_client: PatchedClient
) -> None:
    """A priced window gets a money sensor unit-ed with AxonHub's currency."""
    patched_client.channels = [node(9, "Priced", "commandcode", "warning", PRICED_DATA)]
    entry = make_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert patched_client.currency_reads == 1

    used = hass.states.get("sensor.priced_monthly_used")
    assert used is not None, sorted(hass.states.async_entity_ids())
    assert used.state == "42.0"
    # The amount stays an attribute too, for templates that already read it.
    assert used.attributes["period_cost"] == 1.25

    cost = hass.states.get("sensor.priced_monthly_cost")
    assert cost is not None
    assert cost.state == "1.25"
    assert cost.attributes["device_class"] == "monetary"
    assert cost.attributes["state_class"] == "total"
    assert cost.attributes["unit_of_measurement"] == "USD"
    assert cost.attributes["period_start"] == "2026-01-01T00:00:00+00:00"


async def test_cost_entities_are_hidden_without_currency(
    hass: HomeAssistant, patched_client: PatchedClient
) -> None:
    """No reported currency means no money sensor, rather than a unitless one."""
    patched_client.channels = [node(9, "Priced", "commandcode", "warning", PRICED_DATA)]
    patched_client.currency = None

    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.priced_monthly_cost") is None
    # The percentage of the same window is unaffected.
    assert hass.states.get("sensor.priced_monthly_used").state == "42.0"


async def test_instance_ratios_are_entities(
    hass: HomeAssistant, patched_client: PatchedClient
) -> None:
    """The success and cache hit rates are chartable sensors, not attributes."""
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    success = hass.states.get("sensor.axon_local_8090_success_rate")
    assert success is not None, sorted(hass.states.async_entity_ids())
    assert success.state == "99.12"
    assert success.attributes["unit_of_measurement"] == "%"
    assert success.attributes["state_class"] == "measurement"

    # 40000 cached of (120000 input + 40000 cached) prompt tokens.
    cache = hass.states.get("sensor.axon_local_8090_cache_hit_rate_today")
    assert cache is not None
    assert cache.state == "25.0"
    assert cache.attributes["unit_of_measurement"] == "%"
    assert cache.attributes["input_tokens"] == 120000
    assert cache.attributes["cached_tokens"] == 40000

    # All four token windows get their own rate.
    for window in ("today", "this_week", "this_month", "all_time"):
        assert hass.states.get(f"sensor.axon_local_8090_cache_hit_rate_{window}")


async def test_window_entity_names_follow_the_language(
    hass: HomeAssistant, patched_client: PatchedClient
) -> None:
    """Window names are translated, while entity ids stay English.

    The name comes from a translation key with the provider's window identifier
    as a placeholder, so Chinese reads "5h 窗口已用" without renaming anything:
    Home Assistant builds entity ids from the translations of the language in
    ``languages.NATIVE_ENTITY_IDS`` (zh-Hans is not one), which keeps ids
    stable and English for every user.
    """
    hass.config.language = "zh-Hans"

    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    expected = {
        "sensor.claude_max_5h_window_used": "Claude Max 5h 窗口已用额度",
        "sensor.claude_max_5h_window_reset": "Claude Max 5h 窗口重置时间",
        "sensor.claude_max_5h_window_time_elapsed": "Claude Max 5h 窗口已用时间",
        "sensor.chatgpt_plus_primary_window_used": "ChatGPT Plus Primary 窗口已用额度",
        # A bucket style window keeps its provider identifier verbatim.
        "sensor.copilot_premium_interactions_used": (
            "Copilot premium_interactions 已用额度"
        ),
    }
    for entity_id, name in expected.items():
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        assert state.attributes["friendly_name"] == name


async def test_window_entity_names_fall_back_to_english(
    hass: HomeAssistant, patched_client: PatchedClient
) -> None:
    """A language without its own file reads the English names.

    Home Assistant always loads English as the base and overlays the requested
    language on top, so shipping only en + zh-Hans does not leave other
    languages with unnamed entities.
    """
    hass.config.language = "de"

    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    used = hass.states.get("sensor.claude_max_5h_window_used")
    assert used is not None
    assert used.attributes["friendly_name"] == "Claude Max 5h window used"
    # ...and the entity id is still the English one.
    assert hass.states.get("sensor.claude_max_5h_window_used") is not None


async def test_window_time_entity_reports_elapsed_not_remaining(
    hass: HomeAssistant, patched_client: PatchedClient
) -> None:
    """The window time sensor counts up, so it pairs with the used percentage.

    A window that is one quarter through must read 25, not 75: the two sensors
    then say "40% of the quota gone, 25% of the time gone" and comparing them
    needs no mental inversion.
    """
    now = dt_util.utcnow()
    live = {
        "subscription_status": "active",
        "_limits": [
            {
                "type": "token",
                "status": "available",
                "usageRatio": 0.4,
                "ready": True,
                "window": "5h",
                "nextResetAt": (now + timedelta(hours=3)).isoformat(),
                "periodStart": (now - timedelta(hours=1)).isoformat(),
            }
        ],
    }
    patched_client.channels = [node(9, "Live", "claudecode", "available", live)]

    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    used = hass.states.get("sensor.live_5h_window_used")
    elapsed = hass.states.get("sensor.live_5h_window_time_elapsed")
    assert used is not None and elapsed is not None

    assert used.state == "40.0"
    # 1 of 4 hours gone. Were this still the remaining side it would be 75.
    assert elapsed.state == "25.0"
    assert elapsed.attributes["state_class"] == "measurement"


async def test_commandcode_monthly_reset_matches_the_axonhub_ui(
    hass: HomeAssistant, patched_client: PatchedClient
) -> None:
    """The monthly reset entity shows the subscription period end.

    AxonHub normalizes CommandCode's monthly limit with the 5h/weekly reset
    time, so the entity read "later today" instead of the real month end and
    disagreed with the AxonHub UI, which renders the monthly row from the raw
    ``current_period_end``.
    """
    payload = {
        "current_period_end": "2026-02-01T12:02:05.000Z",
        "_limits": [
            {
                "type": "subscription_cycle",
                "window": "5h",
                "status": "available",
                "ready": True,
                "usageRatio": 0.02,
                "nextResetAt": "2026-01-10T14:00:00Z",
                "periodStart": "2026-01-10T09:00:00Z",
            },
            {
                "type": "subscription_cycle",
                "window": "weekly",
                "status": "available",
                "ready": True,
                "usageRatio": 0.31,
                "nextResetAt": "2026-01-12T15:00:00Z",
                "periodStart": "2026-01-05T15:00:00Z",
            },
            {
                "type": "subscription_cycle",
                "window": "monthly",
                "status": "available",
                "ready": True,
                "usageRatio": 0.65,
                "nextResetAt": "2026-01-10T14:00:00Z",
                "periodStart": None,
            },
        ],
    }
    patched_client.channels = [
        node(9, "Command Code", "commandcode", "available", payload)
    ]

    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    monthly_reset = hass.states.get("sensor.command_code_monthly_reset")
    assert monthly_reset is not None, sorted(hass.states.async_entity_ids())
    assert monthly_reset.state == "2026-02-01T12:02:05+00:00"

    # The 5h window keeps its own reset rather than inheriting the monthly one.
    five_hour_reset = hass.states.get("sensor.command_code_5h_window_reset")
    assert five_hour_reset is not None
    assert five_hour_reset.state == "2026-01-10T14:00:00+00:00"
