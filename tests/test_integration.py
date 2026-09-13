"""Tests for the AxonHub custom integration.

The AxonHub HTTP layer is exercised against a real mock server in
``test_api.py``; here the client methods are patched so the tests focus on the
Home Assistant plumbing: config flow, coordinator, entity/device creation,
services and re-authentication.
"""

from __future__ import annotations

from typing import Any

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
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


class PatchedClient:
    """Records which client methods the integration called."""

    def __init__(self) -> None:
        self.signins = 0
        self.channel_reads = 0
        self.checks = 0
        self.fail_auth = False
        self.api_error: str | None = None


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
        return CHANNELS

    async def _trigger(self) -> None:
        recorder.checks += 1

    monkeypatch.setattr(AxonHubClient, "async_sign_in", _sign_in)
    monkeypatch.setattr(AxonHubClient, "async_get_channels", _get_channels)
    monkeypatch.setattr(AxonHubClient, "async_trigger_quota_check", _trigger)
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
    assert patched_client.signins == 1
    # Validation reads the channels once; Home Assistant then sets the freshly
    # created entry up, which reads them again.
    assert patched_client.channel_reads >= 1


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
    assert five_hour.attributes["reset_at"].startswith("2026-01-01")

    seven_day = hass.states.get("sensor.claude_max_7d_window_used")
    assert seven_day is not None
    assert seven_day.state == "41.0"

    # The empty "overage" window must not create an entity.
    assert hass.states.get("sensor.claude_max_overage_window_used") is None

    reset = hass.states.get("sensor.claude_max_next_quota_reset")
    assert reset is not None
    assert reset.state == "2026-01-01T00:00:00+00:00"

    ready = hass.states.get("binary_sensor.claude_max_quota_ready")
    assert ready is not None
    assert ready.state == "on"

    # Codex channel.
    assert hass.states.get("sensor.chatgpt_plus_primary_window_used").state == "43.5"
    assert hass.states.get("sensor.chatgpt_plus_secondary_window_used").state == "12.0"

    # Copilot channel.
    premium = hass.states.get("sensor.copilot_premium_interactions_remaining")
    assert premium is not None
    assert premium.state == "12.5"
    assert premium.attributes["entitlement"] == 300.0

    # Channels without a quota checker are ignored entirely.
    assert not [
        entity_id
        for entity_id in hass.states.async_entity_ids()
        if "plain_anthropic" in entity_id
    ]

    devices = dr.async_get(hass)
    channel_devices = [
        device
        for device in devices.devices
        if device.name in {"Claude Max", "ChatGPT Plus", "Copilot"}
    ]
    assert len(channel_devices) == 3, [device.name for device in channel_devices]
    assert all(
        entry.entry_id in device.config_entries for device in channel_devices
    ), "channel devices must belong to the config entry"

    hub = next(
        device
        for device in devices.devices
        if (DOMAIN, entry.entry_id) in device.identifiers
    )
    assert hub.name == "axon.local:8090"
    assert hub.configuration_url == BASE_URL


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
