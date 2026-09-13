"""Binary sensor platform for AxonHub provider quotas."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import AxonHubQuotaCoordinator
from .entity import AxonHubEntity, AxonHubEntityManager


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the AxonHub binary sensors."""
    coordinator: AxonHubQuotaCoordinator = hass.data[DOMAIN][entry.entry_id]

    manager = AxonHubEntityManager(coordinator, async_add_entities, _build_entities)
    await manager.async_sync()
    entry.async_on_unload(coordinator.async_add_listener(manager.async_schedule_sync))


def _build_entities(
    coordinator: AxonHubQuotaCoordinator,
) -> dict[str, BinarySensorEntity]:
    """Build one readiness binary sensor per quota-enabled channel."""
    return {
        f"{channel_key}_ready": AxonHubQuotaReadySensor(coordinator, channel_key)
        for channel_key in (coordinator.data or {})
    }


class AxonHubQuotaReadySensor(AxonHubEntity, BinarySensorEntity):
    """Whether the channel still has usable provider quota.

    AxonHub reports ``ready`` as true for the ``available`` and ``warning``
    statuses and false for ``exhausted`` and ``unknown``.
    """

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_translation_key = "quota_ready"

    def __init__(self, coordinator: AxonHubQuotaCoordinator, channel_key: str) -> None:
        """Initialize the readiness binary sensor."""
        super().__init__(coordinator, channel_key)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{channel_key}_ready"

    @property
    def is_on(self) -> bool | None:
        """Return True while the channel can still serve requests."""
        channel = self.channel
        if channel is None:
            return None
        return channel.ready

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Expose the status behind the boolean."""
        channel = self.channel
        if channel is None:
            return {}
        return {
            "status": channel.status,
            "provider": channel.provider,
            "channel_name": channel.name,
        }
