"""Data update coordinators for the AxonHub integration."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    ConfigEntryAuthFailed,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import AxonHubApiError, AxonHubAuthError, AxonHubClient, AxonHubError
from .const import DOMAIN
from .quota import ChannelQuota, build_channel_quota
from .stats import AxonHubStats, build_stats

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


class AxonHubStatsCoordinator(DataUpdateCoordinator[AxonHubStats | None]):
    """Poll AxonHub's instance wide request and token statistics.

    Kept separate from the quota coordinator on purpose. Quota data needs
    ``read:channels``, the dashboard aggregates need ``read:dashboard``, and a
    deployment can well have one without the other. Sharing a single coordinator
    would turn "this account may not read the dashboard" into "every quota
    sensor is unavailable", so a failure here leaves the quota entities alone.
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
            name=f"{DOMAIN}_stats",
            config_entry=entry,
            update_interval=scan_interval,
            always_update=True,
        )
        self.entry = entry
        self.client = client

    async def _async_update_data(self) -> AxonHubStats | None:
        """Fetch and normalize the dashboard aggregates."""
        try:
            payload: dict[str, Any] = await self.client.async_get_dashboard_stats()
        except AxonHubAuthError as err:
            # The credentials are fine (quota polling uses the same ones), the
            # account just may not read the dashboard: report "unavailable"
            # rather than dragging the whole config entry into reauth.
            _LOGGER.debug("AxonHub rejected the dashboard statistics: %s", err)
            return None
        except AxonHubApiError as err:
            _LOGGER.debug(
                "AxonHub would not answer the dashboard statistics, hiding the "
                "instance sensors: %s",
                err,
            )
            return None
        except AxonHubError as err:
            # Transport level failures are transient (AxonHub restarting, Wi-Fi
            # dropping): raise so Home Assistant retries and logs it once.
            raise UpdateFailed(str(err)) from err

        stats = build_stats(payload)
        if stats is None:
            _LOGGER.debug("AxonHub returned no dashboard statistics")

        return stats
