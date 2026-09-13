"""Data update coordinator for the AxonHub integration."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    ConfigEntryAuthFailed,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import AxonHubAuthError, AxonHubClient, AxonHubError
from .const import DOMAIN
from .quota import ChannelQuota, build_channel_quota

_LOGGER = logging.getLogger(__name__)


class AxonHubQuotaCoordinator(DataUpdateCoordinator[dict[str, ChannelQuota]]):
    """Poll AxonHub for the cached provider quota status of every channel.

    AxonHub itself refreshes the provider quota every
    ``provider_quota.check_interval`` (20 minutes by default), so polling the
    cached status is cheap: no upstream provider API is contacted.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: AxonHubClient,
        scan_interval: timedelta,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=scan_interval,
            always_update=True,
        )
        self.entry = entry
        self.client = client

    async def _async_update_data(self) -> dict[str, ChannelQuota]:
        """Fetch the channel list and normalize the quota payloads."""
        try:
            nodes = await self.client.async_get_channels()
        except AxonHubAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except AxonHubError as err:
            raise UpdateFailed(str(err)) from err

        channels: dict[str, ChannelQuota] = {}
        for node in nodes:
            channel = build_channel_quota(node)
            if channel is not None:
                channels[channel.channel_key] = channel

        _LOGGER.debug(
            "AxonHub reported %d quota-enabled channel(s) out of %d channel(s)",
            len(channels),
            len(nodes),
        )

        return channels

    async def async_trigger_provider_check(self) -> None:
        """Force AxonHub to re-check every provider quota, then refresh."""
        await self.client.async_trigger_quota_check()
        await self.async_request_refresh()
