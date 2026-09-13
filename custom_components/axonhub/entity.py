"""Shared entity plumbing for the AxonHub integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BASE_URL, DOMAIN
from .coordinator import AxonHubQuotaCoordinator
from .quota import ChannelQuota


class AxonHubEntity(CoordinatorEntity[AxonHubQuotaCoordinator]):
    """Base class for entities belonging to a single AxonHub channel."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AxonHubQuotaCoordinator, channel_key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._channel_key = channel_key

    @property
    def channel(self) -> ChannelQuota | None:
        """Return the quota status of the channel this entity belongs to."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get(self._channel_key)

    @property
    def available(self) -> bool:
        """Return False when the channel is gone or the poll failed."""
        return super().available and self.channel is not None

    @property
    def device_info(self) -> DeviceInfo:
        """Group every entity of one channel into its own device.

        Home Assistant links the device to the config entry automatically, so
        no explicit ``via_device`` link to the hub device is needed (that
        parameter is deprecated since 2026.9).
        """
        channel = self.channel
        entry = self.coordinator.entry
        return DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{self._channel_key}")},
            name=channel.name if channel else self._channel_key,
            manufacturer="AxonHub",
            model=f"AxonHub channel ({channel.provider})"
            if channel
            else "AxonHub channel",
            configuration_url=entry.data.get(CONF_BASE_URL),
        )


EntityBuilder = Callable[[AxonHubQuotaCoordinator], "dict[str, Entity]"]


class AxonHubEntityManager:
    """Keep dynamically discovered entities in sync with the coordinator data.

    The set of entities depends on runtime data (which channels exist and which
    quota windows their provider reports), so entities are created and removed
    on every coordinator update instead of once at setup time.
    """

    def __init__(
        self,
        coordinator: AxonHubQuotaCoordinator,
        async_add_entities: AddEntitiesCallback,
        builder: EntityBuilder,
    ) -> None:
        """Initialize the manager."""
        self._coordinator = coordinator
        self._async_add_entities = async_add_entities
        self._builder = builder
        self._entities: dict[str, Entity] = {}
        self._lock = asyncio.Lock()

    async def async_sync(self) -> None:
        """Add entities for new data and remove entities for vanished data."""
        async with self._lock:
            wanted = self._builder(self._coordinator)

            new_keys = [key for key in wanted if key not in self._entities]
            if new_keys:
                new_entities = [wanted[key] for key in new_keys]
                self._entities.update({key: wanted[key] for key in new_keys})
                self._async_add_entities(new_entities)

            stale_keys = [key for key in self._entities if key not in wanted]
            stale_entities = [self._entities.pop(key) for key in stale_keys]

        for entity in stale_entities:
            await entity.async_remove()

    @callback
    def async_schedule_sync(self) -> None:
        """Schedule a sync from a coordinator listener."""
        self._coordinator.hass.async_create_task(self.async_sync())
