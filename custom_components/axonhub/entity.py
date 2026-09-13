"""Shared entity plumbing for the AxonHub integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BASE_URL, DOMAIN
from .coordinator import AxonHubQuotaCoordinator, AxonHubStatsCoordinator
from .quota import ChannelQuota


def hub_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return the device info of the AxonHub instance itself.

    The same identifiers are used by ``__init__.py`` when it creates the hub
    device, so the instance sensors land on that device instead of creating a
    second one next to it.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="AxonHub",
        model="AI gateway",
        configuration_url=entry.data.get(CONF_BASE_URL),
    )


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


class AxonHubHubEntity(CoordinatorEntity[AxonHubStatsCoordinator]):
    """Base class for instance wide entities attached to the AxonHub device.

    These carry no channel; they describe the AxonHub instance as a whole, which
    is why they live on the hub device ``__init__.py`` registers.
    """

    _attr_has_entity_name = True

    # Entities whose only job is to report whether the statistics are there must
    # stay available while they are missing, everyone else goes unavailable.
    _requires_stats = True

    def __init__(
        self, coordinator: AxonHubStatsCoordinator, unique_suffix: str
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{unique_suffix}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return the AxonHub instance device."""
        return hub_device_info(self.coordinator.entry)

    @property
    def available(self) -> bool:
        """Return False while AxonHub will not answer the dashboard query.

        The coordinator keeps ``last_update_success`` true and stores ``None``
        when the account lacks the ``read:dashboard`` scope, so the state comes
        from the payload rather than from the update result.
        """
        if not super().available:
            return False
        return self.stats is not None or not self._requires_stats

    @property
    def stats(self):
        """Return the latest instance statistics, if AxonHub answered."""
        return self.coordinator.data


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
