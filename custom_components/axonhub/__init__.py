"""The AxonHub integration.

Exposes the provider quota status (Claude Code, Codex, GitHub Copilot, NanoGPT)
that AxonHub tracks for each channel as Home Assistant sensors.
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AxonHubClient, AxonHubError
from .config_flow import scan_interval_for_entry, verify_ssl_for_entry
from .const import ATTR_ENTRY_ID, CONF_BASE_URL, DOMAIN, SERVICE_REFRESH_QUOTAS
from .coordinator import AxonHubQuotaCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

REFRESH_QUOTAS_SCHEMA = vol.Schema({vol.Optional(ATTR_ENTRY_ID): cv.string})


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up AxonHub (config entry only)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up AxonHub from a config entry."""
    session = async_get_clientsession(hass, verify_ssl=verify_ssl_for_entry(entry))
    client = AxonHubClient(
        session,
        entry.data[CONF_BASE_URL],
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
    )
    coordinator = AxonHubQuotaCoordinator(
        hass, entry, client, scan_interval_for_entry(entry)
    )

    # Raises ConfigEntryAuthFailed (which starts the reauth flow) or
    # ConfigEntryNotReady on the first failure.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Register a device for the AxonHub instance itself; every channel device
    # shows up under the same config entry.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="AxonHub",
        model="AI gateway",
        configuration_url=entry.data.get(CONF_BASE_URL),
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    _async_register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an AxonHub config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    coordinators: dict[str, AxonHubQuotaCoordinator] = hass.data.get(DOMAIN, {})
    coordinators.pop(entry.entry_id, None)

    if not coordinators:
        hass.services.async_remove(DOMAIN, SERVICE_REFRESH_QUOTAS)
        hass.data.pop(DOMAIN, None)

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry so option changes take effect."""
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register the integration services once."""
    if hass.services.has_service(DOMAIN, SERVICE_REFRESH_QUOTAS):
        return

    async def _async_handle_refresh(call: ServiceCall) -> None:
        coordinators: dict[str, AxonHubQuotaCoordinator] = hass.data.get(DOMAIN, {})
        entry_id = call.data.get(ATTR_ENTRY_ID)

        if entry_id:
            coordinator = coordinators.get(entry_id)
            if coordinator is None:
                raise ServiceValidationError(
                    f"Unknown AxonHub config entry: {entry_id}"
                )
            targets = [coordinator]
        else:
            targets = list(coordinators.values())

        if not targets:
            raise ServiceValidationError("AxonHub is not configured")

        for coordinator in targets:
            try:
                await coordinator.async_trigger_provider_check()
            except AxonHubError as err:
                raise HomeAssistantError(
                    f"Failed to refresh AxonHub provider quotas: {err}"
                ) from err

    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_QUOTAS,
        _async_handle_refresh,
        schema=REFRESH_QUOTAS_SCHEMA,
    )
