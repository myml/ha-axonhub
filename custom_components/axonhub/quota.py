"""Translate AxonHub provider quota payloads into Home Assistant metrics.

AxonHub stores one provider-specific JSON blob per channel
(``Channel.providerQuotaStatus.quotaData``). The shape differs per provider, so
this module normalizes each of them into a list of :class:`QuotaMetric` objects
that the sensor platform can expose one-to-one.

Reference (AxonHub repository, ``internal/server/biz/provider_quota/``):

* ``claudecode_checker.go`` – ``windows.<5h|7d|overage>.{utilization,reset,status}``
  where ``utilization`` is a 0..1 ratio and ``reset`` a unix timestamp.
* ``codex_checker.go`` – ``rate_limit.<primary_window|secondary_window>.used_percent``
  in percent plus ``reset_at`` / ``reset_after_seconds`` / ``limit_window_seconds``.
* ``github_copilot_checker.go`` – ``quota_snapshots.<name>.percent_remaining`` in
  percent, falling back to ``limited_user_quotas`` / ``total_quotas``.
* ``nanogpt_checker.go`` – ``windows.<key>.percentUsed`` as a 0..1 ratio and
  ``resetAt`` as a millisecond timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.const import PERCENTAGE
from homeassistant.util import dt as dt_util

from .const import SUPPORTED_CHANNEL_TYPES

STATUS_UNKNOWN = "unknown"


@dataclass(slots=True)
class QuotaMetric:
    """A single numeric quota value for a channel."""

    key: str
    """Stable identifier, unique per channel."""

    name: str
    """English entity name, following Home Assistant conventions."""

    value: float | None
    """Numeric value; ``None`` when the provider reported no usable number."""

    unit: str | None = None
    icon: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChannelQuota:
    """Normalized provider quota status of one AxonHub channel."""

    channel_id: str
    channel_key: str
    name: str
    channel_type: str
    status: str
    ready: bool
    next_reset_at: datetime | None
    next_check_at: datetime | None
    quota_data: dict[str, Any]
    metrics: list[QuotaMetric]
    error: str | None = None

    @property
    def provider(self) -> str:
        """Return the provider type as AxonHub reports it."""
        return self.channel_type

    def metric(self, key: str) -> QuotaMetric | None:
        """Return one metric by key."""
        for metric in self.metrics:
            if metric.key == key:
                return metric
        return None


def build_channel_quota(node: dict[str, Any]) -> ChannelQuota | None:
    """Build a :class:`ChannelQuota` from a ``queryChannels`` node.

    Returns ``None`` for channels AxonHub does not run a quota checker for.
    """
    channel_type = str(node.get("type") or "").lower()
    if channel_type not in SUPPORTED_CHANNEL_TYPES:
        return None

    channel_id = str(node.get("id") or "")
    if not channel_id:
        return None

    status_object = node.get("providerQuotaStatus")
    if not isinstance(status_object, dict):
        status_object = {}

    quota_data = status_object.get("quotaData")
    if not isinstance(quota_data, dict):
        quota_data = {}

    status = str(status_object.get("status") or STATUS_UNKNOWN).lower()

    return ChannelQuota(
        channel_id=channel_id,
        channel_key=channel_key(channel_id),
        name=str(node.get("name") or channel_id),
        channel_type=channel_type,
        status=status,
        ready=bool(status_object.get("ready", False)),
        next_reset_at=_parse_time(status_object.get("nextResetAt")),
        next_check_at=_parse_time(status_object.get("nextCheckAt")),
        quota_data=quota_data,
        metrics=build_metrics(channel_type, quota_data),
        error=_as_text(quota_data.get("error")),
    )


def channel_key(channel_id: str) -> str:
    """Turn ``gid://axonhub/channel/12`` into the stable key ``channel_12``."""
    cleaned = channel_id.strip().rstrip("/")
    if cleaned.startswith("gid://axonhub/"):
        cleaned = cleaned[len("gid://axonhub/") :]

    parts = [part for part in cleaned.split("/") if part]
    if len(parts) >= 2:
        return f"{parts[0]}_{parts[1]}"

    return cleaned.replace("/", "_").replace(":", "_")


def build_metrics(channel_type: str, quota_data: dict[str, Any]) -> list[QuotaMetric]:
    """Build the provider specific metric list."""
    if channel_type == "claudecode":
        return _claudecode_metrics(quota_data)
    if channel_type == "codex":
        return _codex_metrics(quota_data)
    if channel_type == "github_copilot":
        return _copilot_metrics(quota_data)
    if channel_type in ("nanogpt", "nanogpt_responses"):
        return _nanogpt_metrics(quota_data)
    return []


_CLAUDE_WINDOW_LABELS = {
    "5h": "5h window",
    "7d": "7d window",
    "overage": "Overage window",
}

_CODEX_WINDOW_LABELS = {
    "primary_window": "Primary window",
    "secondary_window": "Secondary window",
}

_NANOGPT_WINDOW_LABELS = {
    "weeklyInputTokens": "Weekly input tokens",
    "dailyInputTokens": "Daily input tokens",
    "dailyImages": "Daily images",
}


def _claudecode_metrics(quota_data: dict[str, Any]) -> list[QuotaMetric]:
    """Claude Code reports a 0..1 utilization ratio per rate limit window."""
    windows = quota_data.get("windows")
    if not isinstance(windows, dict):
        return []

    metrics: list[QuotaMetric] = []
    for window_key, label in _CLAUDE_WINDOW_LABELS.items():
        window = windows.get(window_key)
        if not isinstance(window, dict):
            continue

        utilization = _as_float(window.get("utilization"))
        status = _as_text(window.get("status"))
        reset = _as_int(window.get("reset"))
        if not status and not reset and not utilization:
            # The rate limit headers were absent for this window, so AxonHub
            # stored an empty entry; nothing worth exposing.
            continue

        attributes: dict[str, Any] = {
            "window": window_key,
            "status": status,
        }
        if reset:
            attributes["reset"] = reset
            attributes["reset_at"] = _from_unix_seconds(reset)

        metrics.append(
            QuotaMetric(
                key=f"window_{window_key}",
                name=f"{label} used",
                value=_round(utilization * 100) if utilization is not None else None,
                unit=PERCENTAGE,
                icon="mdi:percent",
                attributes=attributes,
            )
        )

    return metrics


def _codex_metrics(quota_data: dict[str, Any]) -> list[QuotaMetric]:
    """Codex reports ``used_percent`` (0..100) per rate limit window."""
    rate_limit = quota_data.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return []

    metrics: list[QuotaMetric] = []
    for window_key, label in _CODEX_WINDOW_LABELS.items():
        window = rate_limit.get(window_key)
        if not isinstance(window, dict):
            continue

        used_percent = _as_float(window.get("used_percent"))
        if used_percent is None:
            continue

        attributes: dict[str, Any] = {
            "window": window_key,
            "plan_type": _as_text(quota_data.get("plan_type")),
        }
        reset_at = _as_int(window.get("reset_at"))
        if reset_at:
            attributes["reset_at"] = _from_unix_seconds(reset_at)
        reset_after = _as_int(window.get("reset_after_seconds"))
        if reset_after:
            attributes["reset_after_seconds"] = reset_after
        window_seconds = _as_int(window.get("limit_window_seconds"))
        if window_seconds:
            attributes["limit_window_seconds"] = window_seconds
        if "limit_reached" in window:
            attributes["limit_reached"] = bool(window.get("limit_reached"))

        metrics.append(
            QuotaMetric(
                key=window_key.removesuffix("_window"),
                name=f"{label} used",
                value=_round(used_percent),
                unit=PERCENTAGE,
                icon="mdi:percent",
                attributes=attributes,
            )
        )

    return metrics


def _copilot_metrics(quota_data: dict[str, Any]) -> list[QuotaMetric]:
    """Copilot reports ``percent_remaining`` (0..100) per quota snapshot."""
    metrics: list[QuotaMetric] = []

    snapshots = quota_data.get("quota_snapshots")
    if isinstance(snapshots, dict):
        for snapshot_name, snapshot in snapshots.items():
            if not isinstance(snapshot, dict):
                continue

            unlimited = bool(snapshot.get("unlimited"))
            remaining = _as_float(snapshot.get("percent_remaining"))
            if not unlimited and remaining is None:
                continue

            attributes: dict[str, Any] = {
                "quota_id": _as_text(snapshot.get("quota_id")) or snapshot_name,
                "unlimited": unlimited,
            }
            entitlement = _as_float(snapshot.get("entitlement"))
            if entitlement is not None:
                attributes["entitlement"] = entitlement
            quota_remaining = _as_float(snapshot.get("quota_remaining"))
            if quota_remaining is not None:
                attributes["quota_remaining"] = quota_remaining
            reset_at = _as_int(snapshot.get("quota_reset_at"))
            if reset_at:
                attributes["quota_reset_at"] = reset_at
            if snapshot.get("has_quota") is not None:
                attributes["has_quota"] = bool(snapshot.get("has_quota"))

            metrics.append(
                QuotaMetric(
                    key=f"snapshot_{_slug(snapshot_name)}",
                    name=f"{snapshot_name} remaining",
                    value=None if unlimited else _round(remaining),
                    unit=None if unlimited else PERCENTAGE,
                    icon="mdi:battery-heart-variant",
                    attributes=attributes,
                )
            )

    if metrics:
        return metrics

    # Classic (non-snapshot) Copilot accounts only expose raw counters.
    totals = quota_data.get("total_quotas")
    remaining_quotas = quota_data.get("limited_user_quotas")
    if not isinstance(totals, dict) or not isinstance(remaining_quotas, dict):
        return metrics

    for quota_name, total in totals.items():
        remaining = _as_float(remaining_quotas.get(quota_name))
        total_value = _as_float(total)
        if remaining is None:
            continue

        attributes = {"remaining": remaining}
        if total_value is not None:
            attributes["entitlement"] = total_value

        percent = None
        if total_value:
            percent = _round(remaining / total_value * 100)

        metrics.append(
            QuotaMetric(
                key=f"quota_{_slug(quota_name)}",
                name=f"{quota_name} remaining",
                value=percent,
                unit=PERCENTAGE,
                icon="mdi:battery-heart-variant",
                attributes=attributes,
            )
        )

    return metrics


def _nanogpt_metrics(quota_data: dict[str, Any]) -> list[QuotaMetric]:
    """NanoGPT reports a 0..1 ``percentUsed`` per subscription window."""
    windows = quota_data.get("windows")
    if not isinstance(windows, dict):
        return []

    metrics: list[QuotaMetric] = []
    for window_key, label in _NANOGPT_WINDOW_LABELS.items():
        window = windows.get(window_key)
        if not isinstance(window, dict):
            continue

        percent_used = _as_float(window.get("percentUsed"))
        if percent_used is None:
            continue

        attributes: dict[str, Any] = {"window": window_key}
        used = _as_float(window.get("used"))
        if used is not None:
            attributes["used"] = used
        remaining = _as_float(window.get("remaining"))
        if remaining is not None:
            attributes["remaining"] = remaining
        reset_at = _as_int(window.get("resetAt"))
        if reset_at:
            attributes["resetAt"] = reset_at
            attributes["reset_at"] = _from_unix_millis(reset_at)

        metrics.append(
            QuotaMetric(
                key=f"window_{_slug(window_key)}",
                name=f"{label} used",
                value=_round(percent_used * 100),
                unit=PERCENTAGE,
                icon="mdi:percent",
                attributes=attributes,
            )
        )

    return metrics


def _as_float(value: Any) -> float | None:
    """Coerce a JSON value to float, ignoring booleans and junk."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> int | None:
    """Coerce a JSON value to int, ignoring booleans and junk."""
    number = _as_float(value)
    if number is None:
        return None
    return int(number)


def _as_text(value: Any) -> str | None:
    """Coerce a JSON value to a non-empty string."""
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    return text or None


def _round(value: float | None, digits: int = 1) -> float | None:
    """Round a percentage for display."""
    if value is None:
        return None
    return round(value, digits)


def _slug(value: str) -> str:
    """Build a stable, entity-key safe slug."""
    return "".join(
        char if char.isalnum() else "_" for char in value.strip().lower()
    ).strip("_") or "value"


def _parse_time(value: Any) -> datetime | None:
    """Parse a GraphQL ``Time`` value into an aware UTC datetime."""
    if not isinstance(value, str) or not value:
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.UTC)
    return dt_util.as_utc(parsed)


def _from_unix_seconds(timestamp: int) -> str | None:
    """Render a unix-seconds timestamp as an ISO 8601 string."""
    try:
        return dt_util.utc_from_timestamp(timestamp).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _from_unix_millis(timestamp: int) -> str | None:
    """Render a unix-milliseconds timestamp as an ISO 8601 string."""
    return _from_unix_seconds(timestamp / 1000)
