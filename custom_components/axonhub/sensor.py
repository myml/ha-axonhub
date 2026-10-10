"""Sensor platform for AxonHub provider quotas.

Two groups of sensors live here:

* one set per quota-enabled channel (devices named after the channel), created
  and removed dynamically from the coordinator payload. Every quota window the
  provider reports becomes a percentage sensor plus, when the provider gives a
  reset time for that window, a timestamp sensor; the status sensor carries the
  channel level state.
* a fixed set for the AxonHub instance itself (request and token counters, plus
  the derived success and cache hit rates), attached to the hub device the
  integration registers.

Numbers that are worth charting are entities rather than attributes: Home
Assistant only records long term statistics for sensors, so anything left on an
attribute (a currency amount, a ratio) can never be plotted over time.
"""

from __future__ import annotations

from datetime import datetime
from functools import partial
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

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

    _async_remove_legacy_entities(hass, entry)

    # The builder needs the instance currency to unit the cost sensors; the
    # entity manager only passes the coordinator, so it is bound here.
    manager = AxonHubEntityManager(
        coordinator,
        async_add_entities,
        partial(_build_entities, currency=runtime.currency),
    )
    await manager.async_sync()
    entry.async_on_unload(coordinator.async_add_listener(manager.async_schedule_sync))

    # The instance sensors are static: which counters exist does not depend on
    # the payload, so they are added once and simply become unavailable while
    # AxonHub will not answer the dashboard query.
    async_add_entities(_build_instance_entities(runtime.stats))


def _async_remove_legacy_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop entities earlier releases registered for this entry.

    Two generations of leftovers accumulate here, and Home Assistant would
    otherwise keep each of them in the registry as a permanently unavailable
    entity:

    * ``_next_reset`` — the channel wide reset entity, until resets became per
      window.
    * ``_remaining`` — the quota-remaining entity, until windows settled on
      exposing the consumed side only. It has to be matched on the entity id as
      well: for Copilot the remaining side *was* the primary entity, so its
      unique id carries no such suffix. ``_time_remaining`` is a different
      entity and must survive, hence the extra check.
    """
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.unique_id.endswith("_next_reset") or _is_removed_remaining(entity):
            registry.async_remove(entity.entity_id)


def _is_removed_remaining(entity: er.RegistryEntry) -> bool:
    """Return True for a leftover quota-remaining entity."""
    for identifier in (entity.unique_id, entity.entity_id):
        if identifier.endswith("_remaining") and not identifier.endswith(
            "_time_remaining"
        ):
            return True
    return False


def _build_instance_entities(
    coordinator: AxonHubStatsCoordinator,
) -> list[SensorEntity]:
    """Build the instance wide request, token and derived ratio sensors."""
    entities: list[SensorEntity] = [
        AxonHubRequestSensor(coordinator, key) for key in REQUEST_SENSORS
    ]
    entities.extend(
        AxonHubTokenSensor(coordinator, window) for window in TOKEN_WINDOWS
    )
    entities.append(AxonHubSuccessRateSensor(coordinator))
    entities.extend(
        AxonHubCacheHitRateSensor(coordinator, window) for window in TOKEN_WINDOWS
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
    currency: str | None = None,
) -> dict[str, SensorEntity]:
    """Build every sensor entity implied by the current coordinator data."""
    entities: dict[str, SensorEntity] = {}

    for channel_key, channel in (coordinator.data or {}).items():
        entities[f"{channel_key}_status"] = AxonHubQuotaStatusSensor(
            coordinator, channel_key
        )
        for metric in channel.metrics:
            # Every window exposes the consumed side only. Copilot reports the
            # remaining side instead, so its entity shows the derived consumed
            # value under the `_used` unique id 0.4.0 already registered for it.
            suffix = "" if metric.used else "_used"
            entities[f"{channel_key}_metric_{metric.key}{suffix}"] = (
                AxonHubQuotaMetricSensor(
                    coordinator, channel_key, metric.key, suffix=suffix
                )
            )
            # Cost is only present for windows AxonHub could price, and only
            # when the instance currency is known to unit it with.
            if metric.period_cost is not None and currency:
                entities[f"{channel_key}_metric_{metric.key}_cost"] = (
                    AxonHubQuotaCostSensor(
                        coordinator, channel_key, metric.key, currency
                    )
                )
            # A window only gets a reset entity when the provider actually
            # reported one for it; otherwise it would sit at `unknown` forever.
            if metric.reset_at is not None:
                entities[f"{channel_key}_metric_{metric.key}_reset"] = (
                    AxonHubQuotaResetSensor(coordinator, channel_key, metric.key)
                )
            # How much of the window is left needs both of its ends, so a window
            # whose start the provider does not pin down gets no such entity.
            if metric.period_start is not None and metric.reset_at is not None:
                entities[f"{channel_key}_metric_{metric.key}_time_remaining"] = (
                    AxonHubQuotaTimeRemainingSensor(
                        coordinator, channel_key, metric.key
                    )
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


class AxonHubMetricEntity(AxonHubEntity):
    """Base class for the entities derived from one quota window.

    A window produces two entities — the percentage and, when the provider
    reports one, its reset timestamp — so the shared lookup and the name
    refresh live here. Home Assistant allows only a single state and a single
    device class per entity, which is why utilization and reset time cannot
    share one.
    """

    def __init__(
        self,
        coordinator: AxonHubQuotaCoordinator,
        channel_key: str,
        metric_key: str,
        suffix: str = "",
    ) -> None:
        """Initialize the metric entity."""
        super().__init__(coordinator, channel_key)
        self._metric_key = metric_key
        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}_{channel_key}_{metric_key}{suffix}"
        )
        self._sync_name()

    @property
    def _metric(self) -> QuotaMetric | None:
        """Return the metric backing this entity."""
        channel = self.channel
        if channel is None:
            return None
        return channel.metric(self._metric_key)

    def _entity_name(self, metric: QuotaMetric) -> str:
        """Return the entity name for the metric."""
        return metric.name

    def _sync_name(self) -> None:
        """Set the entity name from the metric."""
        metric = self._metric
        if metric is not None:
            self._attr_name = self._entity_name(metric)

    def _base_attributes(self, metric: QuotaMetric) -> dict[str, Any]:
        """Return the window attributes shared by both entities."""
        attributes = dict(metric.attributes)
        channel = self.channel
        if channel is not None:
            attributes["channel_name"] = channel.name
            attributes["provider"] = channel.provider
        return attributes

    def _handle_coordinator_update(self) -> None:
        """Refresh the entity name, the metric name may only appear later."""
        self._sync_name()
        super()._handle_coordinator_update()


class AxonHubQuotaMetricSensor(AxonHubMetricEntity, SensorEntity):
    """How much of one quota window has been used, in percent.

    Every window reports the consumed side here, whether the provider reported
    utilisation (Claude Code, Codex, NanoGPT, and the normalized ``_limits``)
    or the remaining headroom (Copilot's snapshots), so a single threshold
    automation works across providers.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: AxonHubQuotaCoordinator,
        channel_key: str,
        metric_key: str,
        suffix: str = "",
    ) -> None:
        """Initialize the quota percentage sensor."""
        super().__init__(coordinator, channel_key, metric_key, suffix=suffix)

    def _entity_name(self, metric: QuotaMetric) -> str:
        """Return the consumed side's entity name."""
        return metric.used_name

    @property
    def native_value(self) -> float | None:
        """Return how much of the window is used."""
        metric = self._metric
        return metric.used_value if metric is not None else None

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
        """Expose provider specific details such as the window reset time.

        ``next_reset_at`` is repeated here as an ISO 8601 string (the reset
        entity carries the same moment as a real timestamp) so a single row can
        show the percentage and its reset time without a second entity.
        """
        metric = self._metric
        if metric is None:
            return {}

        attributes = self._base_attributes(metric)
        if metric.reset_at is not None:
            attributes["next_reset_at"] = metric.reset_at.isoformat()

        return attributes


class AxonHubQuotaResetSensor(AxonHubMetricEntity, SensorEntity):
    """Timestamp at which one quota window resets.

    Providers reset their windows independently (a Claude Code 5h window and
    its 7d window do not expire together), so every window reported with a
    reset gets its own timestamp entity instead of a single channel wide one.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:timer-refresh-outline"

    def __init__(
        self, coordinator: AxonHubQuotaCoordinator, channel_key: str, metric_key: str
    ) -> None:
        """Initialize the reset sensor."""
        super().__init__(coordinator, channel_key, metric_key, suffix="_reset")

    def _entity_name(self, metric: QuotaMetric) -> str:
        """Return the window name with a ``reset`` suffix."""
        return metric.reset_name

    @property
    def native_value(self) -> datetime | None:
        """Return the next reset timestamp of this window."""
        metric = self._metric
        return metric.reset_at if metric is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the window details and the paired percentage value."""
        metric = self._metric
        if metric is None:
            return {}

        attributes = self._base_attributes(metric)
        attributes["value"] = metric.used_value
        attributes["unit"] = metric.unit
        return attributes


class AxonHubQuotaCostSensor(AxonHubMetricEntity, SensorEntity):
    """What one quota window has cost so far, in the instance currency.

    AxonHub prices each limit period from its usage logs, so this exists only
    for windows it could price (it needs the period start and actual spend).
    Being an entity rather than an attribute is what lets Home Assistant keep
    long term statistics for it.

    ``MONETARY`` is the device class Home Assistant requires for money and it
    only permits the ``TOTAL`` state class, so the value is a running total that
    resets with the window rather than a ``total_increasing`` meter.
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:cash"

    def __init__(
        self,
        coordinator: AxonHubQuotaCoordinator,
        channel_key: str,
        metric_key: str,
        currency: str,
    ) -> None:
        """Initialize the cost sensor."""
        super().__init__(coordinator, channel_key, metric_key, suffix="_cost")
        self._attr_native_unit_of_measurement = currency

    def _entity_name(self, metric: QuotaMetric) -> str:
        """Return the window's cost entity name."""
        return metric.cost_name

    @property
    def native_value(self) -> float | None:
        """Return the money spent in this window so far."""
        metric = self._metric
        return metric.period_cost if metric is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the window boundaries the amount was accumulated over."""
        metric = self._metric
        if metric is None:
            return {}

        attributes = self._base_attributes(metric)
        attributes["period_start"] = _isoformat(metric.period_start)
        attributes["reset_at"] = _isoformat(metric.reset_at)
        return attributes


class AxonHubQuotaTimeRemainingSensor(AxonHubMetricEntity, SensorEntity):
    """How much of one quota window is left, in percent.

    Answers "am I burning quota faster than the window refills": 82% of the
    quota used with 60% of the window still ahead reads very differently from
    the same 82% with the window about to reset. Recomputed on every poll from
    the window's start and reset time.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:timer-sand"

    def __init__(
        self, coordinator: AxonHubQuotaCoordinator, channel_key: str, metric_key: str
    ) -> None:
        """Initialize the window time sensor."""
        super().__init__(
            coordinator, channel_key, metric_key, suffix="_time_remaining"
        )

    def _entity_name(self, metric: QuotaMetric) -> str:
        """Return the window's remaining-time entity name."""
        return metric.time_remaining_name

    @property
    def native_value(self) -> float | None:
        """Return how much of the window is still ahead."""
        metric = self._metric
        if metric is None:
            return None
        return metric.time_remaining_percent(dt_util.utcnow())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the window boundaries the share is computed from."""
        metric = self._metric
        if metric is None:
            return {}

        attributes = self._base_attributes(metric)
        attributes["period_start"] = _isoformat(metric.period_start)
        attributes["reset_at"] = _isoformat(metric.reset_at)
        return attributes


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


class AxonHubSuccessRateSensor(AxonHubHubEntity, SensorEntity):
    """Share of tracked requests that did not fail, in percent.

    An entity rather than an attribute of the total so it can be charted over
    time; the same value stays on ``total_requests`` for templates that already
    read it there.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:check-circle-outline"

    def __init__(self, coordinator: AxonHubStatsCoordinator) -> None:
        """Initialize the success rate sensor."""
        super().__init__(coordinator, "requests_success_rate")
        self._attr_translation_key = "requests_success_rate"

    @property
    def native_value(self) -> float | None:
        """Return the share of requests that succeeded."""
        stats = self.stats
        return None if stats is None else stats.requests.success_rate


class AxonHubCacheHitRateSensor(AxonHubHubEntity, SensorEntity):
    """Share of prompt tokens served from the provider cache, in percent.

    Split per window like the token totals, so cache efficiency can be tracked
    over time instead of only being visible as an attribute.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:cached"

    def __init__(
        self, coordinator: AxonHubStatsCoordinator, window: str
    ) -> None:
        """Initialize the cache hit rate sensor."""
        super().__init__(coordinator, f"tokens_{window}_cache_hit_rate")
        self._window = window
        self._attr_translation_key = f"tokens_{window}_cache_hit_rate"

    @property
    def _counts(self) -> TokenCounts | None:
        """Return the token buckets of this window."""
        stats = self.stats
        if stats is None:
            return None
        return getattr(stats, f"tokens_{self._window}", None)

    @property
    def native_value(self) -> float | None:
        """Return the window's cache hit rate."""
        counts = self._counts
        if counts is None:
            return None
        return counts.cache_hit_rate

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the buckets the rate is derived from."""
        counts = self._counts
        if counts is None:
            return {}
        return {
            "input_tokens": counts.input,
            "cached_tokens": counts.cached,
        }
