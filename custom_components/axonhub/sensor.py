"""Sensor platform for AxonHub provider quotas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, STATUS_OPTIONS
from .coordinator import AxonHubQuotaCoordinator
from .entity import AxonHubEntity, AxonHubEntityManager
from .quota import QuotaMetric


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the AxonHub sensors."""
    coordinator: AxonHubQuotaCoordinator = hass.data[DOMAIN][entry.entry_id]

    manager = AxonHubEntityManager(coordinator, async_add_entities, _build_entities)
    await manager.async_sync()
    entry.async_on_unload(coordinator.async_add_listener(manager.async_schedule_sync))


def _build_entities(
    coordinator: AxonHubQuotaCoordinator,
) -> dict[str, SensorEntity]:
    """Build every sensor entity implied by the current coordinator data."""
    entities: dict[str, SensorEntity] = {}

    for channel_key, channel in (coordinator.data or {}).items():
        entities[f"{channel_key}_status"] = AxonHubQuotaStatusSensor(
            coordinator, channel_key
        )
        entities[f"{channel_key}_next_reset"] = AxonHubNextResetSensor(
            coordinator, channel_key
        )
        for metric in channel.metrics:
            entities[f"{channel_key}_metric_{metric.key}"] = AxonHubQuotaMetricSensor(
                coordinator, channel_key, metric.key
            )

    return entities


class AxonHubQuotaStatusSensor(AxonHubEntity, SensorEntity):
    """Overall provider quota status of one channel."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS
    _attr_translation_key = "quota_status"
    _attr_icon = "mdi:gauge"

    def __init__(self, coordinator: AxonHubQuotaCoordinator, channel_key: str) -> None:
        """Initialize the status sensor."""
        super().__init__(coordinator, channel_key)
        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}_{channel_key}_status"
        )

    @property
    def native_value(self) -> str | None:
        """Return available / warning / exhausted / unknown."""
        channel = self.channel
        if channel is None:
            return None
        return channel.status if channel.status in STATUS_OPTIONS else "unknown"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the raw AxonHub payload for templates and automations."""
        channel = self.channel
        if channel is None:
            return {}

        attributes: dict[str, Any] = {
            "channel_id": channel.channel_id,
            "channel_name": channel.name,
            "provider": channel.provider,
            "ready": channel.ready,
            "next_reset_at": _isoformat(channel.next_reset_at),
            "next_check_at": _isoformat(channel.next_check_at),
            "quota_data": channel.quota_data,
        }
        if channel.error:
            attributes["error"] = channel.error

        return attributes


class AxonHubNextResetSensor(AxonHubEntity, SensorEntity):
    """Timestamp at which the primary quota window resets."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "quota_next_reset"
    _attr_icon = "mdi:timer-refresh-outline"

    def __init__(self, coordinator: AxonHubQuotaCoordinator, channel_key: str) -> None:
        """Initialize the reset sensor."""
        super().__init__(coordinator, channel_key)
        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}_{channel_key}_next_reset"
        )

    @property
    def native_value(self) -> datetime | None:
        """Return the next quota reset timestamp."""
        channel = self.channel
        if channel is None:
            return None
        return channel.next_reset_at


class AxonHubQuotaMetricSensor(AxonHubEntity, SensorEntity):
    """One quota window reported by the provider (utilization or remaining)."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: AxonHubQuotaCoordinator,
        channel_key: str,
        metric_key: str,
    ) -> None:
        """Initialize the metric sensor."""
        super().__init__(coordinator, channel_key)
        self._metric_key = metric_key
        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}_{channel_key}_{metric_key}"
        )

        metric = self._metric
        if metric is not None:
            self._attr_name = metric.name

    @property
    def _metric(self) -> QuotaMetric | None:
        """Return the metric backing this entity."""
        channel = self.channel
        if channel is None:
            return None
        return channel.metric(self._metric_key)

    @property
    def native_value(self) -> float | None:
        """Return the metric value."""
        metric = self._metric
        return metric.value if metric is not None else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the metric unit."""
        metric = self._metric
        return metric.unit if metric is not None else PERCENTAGE

    @property
    def icon(self) -> str | None:
        """Return the metric icon."""
        metric = self._metric
        if metric is None:
            return None
        return metric.icon

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose provider specific details such as the window reset time."""
        metric = self._metric
        channel = self.channel
        if metric is None:
            return {}

        attributes = dict(metric.attributes)
        if channel is not None:
            attributes["channel_name"] = channel.name
            attributes["provider"] = channel.provider

        return attributes

    def _handle_coordinator_update(self) -> None:
        """Refresh the entity name, the metric name may only appear later."""
        metric = self._metric
        if metric is not None:
            self._attr_name = metric.name
        super()._handle_coordinator_update()


def _isoformat(value: datetime | None) -> str | None:
    """Render an optional datetime for state attributes."""
    return value.isoformat() if value is not None else None
