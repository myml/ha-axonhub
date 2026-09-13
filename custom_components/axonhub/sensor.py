"""Sensor platform for AxonHub provider quotas.

Two groups of sensors live here:

* one set per quota-enabled channel (devices named after the channel), created
  and removed dynamically from the coordinator payload, and
* a fixed set for the AxonHub instance itself (request and token counters),
  attached to the hub device the integration registers.
"""

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

from . import AxonHubRuntime
from .const import DOMAIN, STATUS_OPTIONS
from .coordinator import AxonHubQuotaCoordinator, AxonHubStatsCoordinator
from .entity import AxonHubEntity, AxonHubEntityManager, AxonHubHubEntity
from .quota import QuotaMetric
from .stats import TokenCounts


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the AxonHub sensors."""
    runtime: AxonHubRuntime = hass.data[DOMAIN][entry.entry_id]
    coordinator = runtime.quota

    manager = AxonHubEntityManager(coordinator, async_add_entities, _build_entities)
    await manager.async_sync()
    entry.async_on_unload(coordinator.async_add_listener(manager.async_schedule_sync))

    # The instance sensors are static: which counters exist does not depend on
    # the payload, so they are added once and simply become unavailable while
    # AxonHub will not answer the dashboard query.
    async_add_entities(_build_instance_entities(runtime.stats))


def _build_instance_entities(
    coordinator: AxonHubStatsCoordinator,
) -> list[SensorEntity]:
    """Build the instance wide request and token sensors."""
    entities: list[SensorEntity] = [
        AxonHubRequestSensor(coordinator, key) for key in REQUEST_SENSORS
    ]
    entities.extend(
        AxonHubTokenSensor(coordinator, window) for window in TOKEN_WINDOWS
    )
    return entities


# Key -> (translation key, icon, cumulative)
#
# `total` and `failed` come from AxonHub's request table and only ever grow,
# while `today`/`this_week`/`this_month` are reset by the calendar — the
# TOTAL_INCREASING state class is designed for exactly that (Home Assistant
# detects the reset and starts a new meter segment). `last_week` is a fixed past
# window, so it oscillates instead and stays a plain measurement.
REQUEST_SENSORS: dict[str, tuple[str, str, bool]] = {
    "total": ("requests_total", "mdi:counter", True),
    "failed": ("requests_failed", "mdi:alert-circle-outline", True),
    "today": ("requests_today", "mdi:calendar-today", True),
    "this_week": ("requests_this_week", "mdi:calendar-week", True),
    "last_week": ("requests_last_week", "mdi:calendar-arrow-left", False),
    "this_month": ("requests_this_month", "mdi:calendar-month", True),
}

# Token windows, each rendered as one "total tokens" sensor whose attributes
# break the number down into input / output / cached and the cache hit rate.
TOKEN_WINDOWS: dict[str, str] = {
    "today": "mdi:calendar-today",
    "this_week": "mdi:calendar-week",
    "this_month": "mdi:calendar-month",
    "all_time": "mdi:chart-timeline-variant",
}


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


class AxonHubRequestSensor(AxonHubHubEntity, SensorEntity):
    """One request counter of the AxonHub instance.

    ``requests_total`` counts every request AxonHub tracked (failures included)
    and ``requests_today``/``_this_week``/``_this_month`` count the requests that
    produced a result. They come from two different AxonHub tables, so they are
    not expected to be equal; see the module docstring of ``stats.py``.
    """

    _attr_native_unit_of_measurement = "requests"
    _attr_suggested_display_precision = 0

    def __init__(
        self, coordinator: AxonHubStatsCoordinator, key: str
    ) -> None:
        """Initialize the request counter sensor."""
        translation_key, icon, cumulative = REQUEST_SENSORS[key]
        super().__init__(coordinator, f"requests_{key}")
        self._key = key
        self._attr_translation_key = translation_key
        self._attr_icon = icon
        self._attr_state_class = (
            SensorStateClass.TOTAL_INCREASING
            if cumulative
            else SensorStateClass.MEASUREMENT
        )

    @property
    def native_value(self) -> int | None:
        """Return the request count of this window."""
        stats = self.stats
        if stats is None:
            return None
        return getattr(stats.requests, self._key, None)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Attach the failure breakdown to the cumulative total sensor."""
        stats = self.stats
        if stats is None or self._key != "total":
            return {}

        attributes: dict[str, Any] = {
            "failed_requests": stats.requests.failed,
            "success_rate": stats.requests.success_rate,
        }
        if stats.requests.average_response_time is not None:
            attributes["average_response_time"] = (
                stats.requests.average_response_time
            )
        return attributes


class AxonHubTokenSensor(AxonHubHubEntity, SensorEntity):
    """Total token usage of one time window.

    The input / output / cached split, and the cache hit rate derived from it,
    are exposed as attributes: cached prompt tokens are kept out of AxonHub's
    ``prompt_tokens`` counter, so the split is what makes the total meaningful.
    """

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "tokens"

    def __init__(
        self, coordinator: AxonHubStatsCoordinator, window: str
    ) -> None:
        """Initialize the token sensor."""
        super().__init__(coordinator, f"tokens_{window}")
        self._window = window
        self._attr_translation_key = f"tokens_{window}"
        self._attr_icon = TOKEN_WINDOWS[window]

    @property
    def _counts(self) -> TokenCounts | None:
        """Return the token buckets of this window."""
        stats = self.stats
        if stats is None:
            return None
        return getattr(stats, f"tokens_{self._window}", None)

    @property
    def native_value(self) -> int | None:
        """Return the window's total token count."""
        counts = self._counts
        return counts.total if counts is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the input / output / cached split behind the total."""
        counts = self._counts
        if counts is None:
            return {}
        return {
            "input_tokens": counts.input,
            "output_tokens": counts.output,
            "cached_tokens": counts.cached,
            "cache_hit_rate": counts.cache_hit_rate,
        }


def _isoformat(value: datetime | None) -> str | None:
    """Render an optional datetime for state attributes."""
    return value.isoformat() if value is not None else None
