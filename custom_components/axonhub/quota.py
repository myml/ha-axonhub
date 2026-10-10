"""Translate AxonHub provider quota payloads into Home Assistant metrics.

AxonHub stores one JSON blob per channel
(``Channel.providerQuotaStatus.quotaData``). Since the "provider quota collection
controls" work (AxonHub PR #2103, first released in v1.0.0-beta8) the backend
normalizes every provider into a provider-agnostic list before storing it, under
``quotaData._limits``::

    {"type": "token", "status": "available", "usageRatio": 0.42, "ready": true,
     "window": "5h", "nextResetAt": "...", "periodStart": "...",
     "periodCost": 1.23, "periodQuota": 5.0}

That is the primary source here, so every provider AxonHub knows about is
covered without keeping a channel-type list in sync. Every limit carries its
own ``nextResetAt``, which is why the reset time is modelled per window
(:attr:`QuotaMetric.reset_at`) instead of only as one channel wide timestamp
(``providerQuotaStatus.nextResetAt`` is just a summary — for Codex it is the
earliest of the windows, see ``codexEarliestFutureReset`` upstream).

The per-provider raw payloads are still parsed as a fallback for older
instances:

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
from datetime import datetime, timedelta
from typing import Any

from homeassistant.const import PERCENTAGE
from homeassistant.util import dt as dt_util

from .const import QUOTA_CHANNEL_TYPES

STATUS_UNKNOWN = "unknown"

# Entity name suffixes. A quota window always exposes both percentages, so the
# builders name one side and derive the other from these.
_USED_SUFFIX = " used"
_REMAINING_SUFFIX = " remaining"

# The word that marks a provider rate limit window ("5h window", "Primary
# window"). Entity names are built from translations, and the placeholder keeps
# the provider's identifier only, so this word is supplied by each language's
# template instead.
_WINDOW_WORD = " window"

# Fixed window lengths, used to derive a window's start when the provider only
# reports its reset. The normalized `_limits` payload carries `periodStart`
# directly, but the per-provider raw payloads do not, and without a start there
# is no way to tell how far through the window we are.
_WINDOW_DURATIONS: dict[str, timedelta] = {
    "5h": timedelta(hours=5),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
}


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

    reset_at: datetime | None = None
    """Next reset of this window.

    Each provider window resets on its own schedule, so this is tracked per
    metric and becomes its own timestamp sensor. ``None`` when the provider
    does not report a reset for this window (Codex omits ``reset_at`` on the
    secondary window in some payloads, the raw strings of an older AxonHub
    instance may not parse, ...).
    """

    used: bool = True
    """Whether :attr:`value` is consumption (``True``) or remaining headroom.

    Every provider reports utilisation except GitHub Copilot, whose snapshots
    carry ``percent_remaining``. Windows expose the consumed side either way,
    so this flag tells :attr:`used_value` whether it has to invert the number.
    """

    period_start: datetime | None = None
    """Start of this window, when the provider pins it down.

    Needed to tell how far through a window we are. The normalized ``_limits``
    payload carries it; the per-provider raw payloads do not, but the windows
    with a fixed length can derive it from :attr:`reset_at`.
    """

    period_cost: float | None = None
    """Money this window cost so far, when AxonHub could price it.

    AxonHub fills ``periodCost`` from its usage logs for every limit whose
    period start it knows, so this is only present for priced windows. It
    becomes a money sensor, unit-ed with the instance currency.
    """

    @property
    def window_label(self) -> str:
        """Return the window label without the used/remaining suffix."""
        for suffix in (_USED_SUFFIX, _REMAINING_SUFFIX):
            if self.name.endswith(suffix):
                return self.name[: -len(suffix)]
        return self.name

    @property
    def window_token(self) -> str:
        """Return the provider's own identifier for this window.

        This is what lands in the ``{window}`` translation placeholder, so it is
        deliberately left as the provider names it (``5h``, ``Primary``,
        ``premium_interactions``, ``Daily images``) rather than translated.
        """
        label = self.window_label
        if label.endswith(_WINDOW_WORD):
            return label[: -len(_WINDOW_WORD)]
        return label

    @property
    def is_rate_limit_window(self) -> bool:
        """Whether this window is one of the provider's rate limit windows.

        Rate limit windows (Claude Code's 5h/7d, Codex's primary/secondary) read
        naturally as "<token> window ...", while the other quota buckets
        (Copilot snapshots, NanoGPT's token windows, a billing cycle) are
        already descriptive. The distinction picks the translation key, which is
        what lets both keep their exact English wording.
        """
        return self.window_label.endswith(_WINDOW_WORD)

    @property
    def used_value(self) -> float | None:
        """Return the consumed percentage.

        Providers report utilisation directly except GitHub Copilot, whose
        remaining percentage is inverted here so every window reads the same
        way. ``None`` when the provider reports no usable number, which leaves
        the entity at ``unknown`` rather than claiming nothing was used.
        """
        if self.value is None:
            return None
        return self.value if self.used else _round(100 - self.value)

    def time_remaining_percent(self, now: datetime) -> float | None:
        """Return how much of the window is left, in percent.

        ``None`` until both ends of the window are known. The share is clamped
        to 0..100 because a poll can land slightly after the stored reset time,
        and a window that has already reset reports nothing left.
        """
        if self.reset_at is None or self.period_start is None:
            return None

        total = (self.reset_at - self.period_start).total_seconds()
        if total <= 0:
            return None

        share = (self.reset_at - now).total_seconds() / total
        return _round(max(0.0, min(1.0, share)) * 100)


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
    provider_type: str | None = None
    account_key: str | None = None

    @property
    def provider(self) -> str:
        """Return the provider AxonHub ran the quota check against.

        ``providerQuotaStatus.providerType`` is authoritative (it maps several
        channel types onto one provider, for example ``opencode_go_anthropic``
        and ``opencode_go``), with the channel type as a fallback for channels
        that have no quota status yet.
        """
        return self.provider_type or self.channel_type

    def metric(self, key: str) -> QuotaMetric | None:
        """Return one metric by key."""
        for metric in self.metrics:
            if metric.key == key:
                return metric
        return None


def build_channel_quota(node: dict[str, Any]) -> ChannelQuota | None:
    """Build a :class:`ChannelQuota` from a ``queryChannels`` node.

    A channel is kept when AxonHub reports a quota status for it, whatever its
    provider type. Channels whose type AxonHub has a quota checker for are also
    kept before their first check has produced data, so they show up as
    ``unknown`` instead of appearing only later.
    """
    channel_id = str(node.get("id") or "")
    if not channel_id:
        return None

    channel_type = str(node.get("type") or "").lower()

    raw_status = node.get("providerQuotaStatus")
    has_status = isinstance(raw_status, dict)

    # Errors AxonHub reported for this channel's `providerQuotaStatus` field,
    # attached by the client when the field failed to resolve.
    field_errors = _as_text_list(node.get("_quota_errors"))

    if not has_status and not field_errors and channel_type not in QUOTA_CHANNEL_TYPES:
        return None

    status_object: dict[str, Any] = raw_status if has_status else {}

    quota_data = status_object.get("quotaData")
    if not isinstance(quota_data, dict):
        quota_data = {}

    status = str(status_object.get("status") or STATUS_UNKNOWN).lower()

    provider_type = _as_text(status_object.get("providerType"))
    if provider_type:
        provider_type = provider_type.lower()

    error = _as_text(quota_data.get("error"))
    if not error and field_errors:
        error = "; ".join(field_errors)

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
        metrics=build_metrics(provider_type or channel_type, quota_data),
        error=error,
        provider_type=provider_type,
        account_key=_as_text(status_object.get("accountKey")),
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
    """Build the metric list for one channel.

    The normalized ``_limits`` array is provider agnostic, so it is preferred;
    the per-provider raw payloads remain as a fallback for older AxonHub
    versions that do not store it.
    """
    normalized = _limits_metrics(quota_data)
    if normalized:
        return normalized

    if channel_type == "claudecode":
        return _claudecode_metrics(quota_data)
    if channel_type == "codex":
        return _codex_metrics(quota_data)
    if channel_type == "github_copilot":
        return _copilot_metrics(quota_data)
    if channel_type in ("nanogpt", "nanogpt_responses"):
        return _nanogpt_metrics(quota_data)
    return []


# Friendly names for the normalized limit windows AxonHub reports. Anything not
# listed here is rendered from its raw value.
_WINDOW_LABELS = {
    "5h": "5h window",
    "7d": "7d window",
    "30d": "30d window",
    "daily": "Daily",
    "weekly": "Weekly",
    "monthly": "Monthly",
    "cycle": "Billing cycle",
    "rolling": "Rolling window",
    "interval": "Interval window",
    "overage": "Overage",
}


def _limits_metrics(quota_data: dict[str, Any]) -> list[QuotaMetric]:
    """Build metrics from AxonHub's normalized ``quotaData._limits`` array.

    ``usageRatio`` is a 0..1 ratio, so it is exposed as a percentage. ``key``
    combines the window and the limit type because providers may report several
    limits of different types in the same window (for example daily tokens and
    daily images).
    """
    raw_limits = quota_data.get("_limits")
    if not isinstance(raw_limits, list):
        return []

    entries: list[tuple[str, str, str, dict[str, Any], float | None]] = []
    for raw in raw_limits:
        if not isinstance(raw, dict):
            continue

        window = _as_text(raw.get("window")) or ""
        limit_type = _as_text(raw.get("type")) or ""
        if not window and not limit_type:
            continue

        label = _WINDOW_LABELS.get(window.lower())
        if label is None:
            label = window.replace("_", " ").strip().title() if window else limit_type.title()

        entries.append((window, limit_type, label, raw, _as_float(raw.get("usageRatio"))))

    # Disambiguate labels when the same window carries several limit types.
    label_counts: dict[str, int] = {}
    for _window, _type, label, _raw, _ratio in entries:
        label_counts[label] = label_counts.get(label, 0) + 1

    metrics: list[QuotaMetric] = []
    used_keys: set[str] = set()
    for window, limit_type, label, raw, ratio in entries:
        name = label
        if label_counts[label] > 1 and limit_type:
            name = f"{label} {limit_type}"

        ratio = _as_float(raw.get("usageRatio"))
        attributes: dict[str, Any] = {
            "window": window or None,
            "type": limit_type or None,
            "status": _as_text(raw.get("status")),
            "ready": raw.get("ready") if isinstance(raw.get("ready"), bool) else None,
            "period_start": _as_text(raw.get("periodStart")),
        }
        period_cost = _as_float(raw.get("periodCost"))
        if period_cost is not None:
            attributes["period_cost"] = period_cost
        period_quota = _as_float(raw.get("periodQuota"))
        if period_quota is not None:
            attributes["period_quota"] = period_quota

        key = f"limit_{_slug(window)}_{_slug(limit_type)}"
        if key in used_keys:
            key = f"{key}_{len(metrics)}"
        used_keys.add(key)

        reset_at = _parse_time(raw.get("nextResetAt"))
        metrics.append(
            QuotaMetric(
                key=key,
                name=f"{name} used",
                value=_round(ratio * 100) if ratio is not None else None,
                unit=PERCENTAGE,
                icon="mdi:percent",
                attributes={k: v for k, v in attributes.items() if v is not None},
                reset_at=reset_at,
                # AxonHub prices a period from its start, so `periodStart` is
                # what makes the daily cost aggregation possible; fall back to
                # the window length for payloads that omit it.
                period_start=_parse_time(raw.get("periodStart"))
                or _window_period_start(reset_at, window),
                period_cost=period_cost,
            )
        )

    return metrics


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

# NanoGPT names its windows after their period instead of a plain duration.
_NANOGPT_WINDOW_DURATIONS = {
    "weeklyInputTokens": timedelta(days=7),
    "dailyInputTokens": timedelta(days=1),
    "dailyImages": timedelta(days=1),
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

        reset_at = _unix_seconds(reset)
        metrics.append(
            QuotaMetric(
                key=f"window_{window_key}",
                name=f"{label} used",
                value=_round(utilization * 100) if utilization is not None else None,
                unit=PERCENTAGE,
                icon="mdi:percent",
                attributes=attributes,
                reset_at=reset_at,
                # The 5h/7d windows have a fixed length, so their start follows
                # from the reset even though this payload omits it.
                period_start=_window_period_start(reset_at, window_key),
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
            attributes["reset_at"] = reset_at
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
                reset_at=_unix_seconds(reset_at),
                # Codex reports the window length outright.
                period_start=_period_start_from_seconds(
                    _unix_seconds(reset_at), window_seconds
                ),
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
                    reset_at=_epoch(reset_at),
                    # Copilot is the one provider whose payload is the remaining
                    # side of the window rather than the consumed side.
                    used=False,
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
                used=False,
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

        reset_at_dt = _unix_millis(reset_at)
        duration = _NANOGPT_WINDOW_DURATIONS.get(window_key)
        metrics.append(
            QuotaMetric(
                key=f"window_{_slug(window_key)}",
                name=f"{label} used",
                value=_round(percent_used * 100),
                unit=PERCENTAGE,
                icon="mdi:percent",
                attributes=attributes,
                reset_at=reset_at_dt,
                period_start=reset_at_dt - duration
                if reset_at_dt is not None and duration is not None
                else None,
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


def _as_text_list(value: Any) -> list[str]:
    """Coerce a JSON value to a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = _as_text(item)
        if text:
            result.append(text)
    return result


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


def _window_period_start(
    reset_at: datetime | None, window: str | None
) -> datetime | None:
    """Derive a window's start from its reset time and known length.

    Only the windows in :data:`_WINDOW_DURATIONS` have a length that is fixed
    by definition; everything else (a billing cycle, a monthly reset date) has
    no derivable start and simply gets no time-remaining entity.
    """
    if reset_at is None or not window:
        return None

    duration = _WINDOW_DURATIONS.get(window.strip().lower())
    if duration is None:
        return None

    return reset_at - duration


def _period_start_from_seconds(
    reset_at: datetime | None, seconds: int | None
) -> datetime | None:
    """Derive a window's start from a provider reported window length."""
    if reset_at is None or not seconds or seconds <= 0:
        return None

    return reset_at - timedelta(seconds=seconds)


def _unix_seconds(timestamp: float | None) -> datetime | None:
    """Turn a unix-seconds timestamp into an aware UTC datetime."""
    if not timestamp:
        return None
    try:
        return dt_util.utc_from_timestamp(timestamp)
    except (OverflowError, OSError, ValueError):
        return None


def _unix_millis(timestamp: float | None) -> datetime | None:
    """Turn a unix-milliseconds timestamp into an aware UTC datetime."""
    if not timestamp:
        return None
    return _unix_seconds(timestamp / 1000)


# Anything above this cannot be a plausible unix-seconds value (1e11 seconds is
# well past year 5000), so it must be milliseconds.
_MILLIS_THRESHOLD = 1e11


def _epoch(timestamp: float | None) -> datetime | None:
    """Turn an epoch timestamp of unknown unit into a datetime.

    GitHub Copilot's ``quota_snapshots`` are passed through to ``quotaData``
    verbatim and AxonHub never reads ``quota_reset_at`` itself, so there is no
    upstream code confirming whether it is seconds or milliseconds. Newer
    AxonHub builds do not need this (their normalized ``_limits`` carries an ISO
    ``nextResetAt``), so the unit is inferred from magnitude rather than
    assumed: the alternative is a reset entity wrong by a factor of 1000.
    """
    if not timestamp:
        return None
    if timestamp >= _MILLIS_THRESHOLD:
        return _unix_millis(timestamp)
    return _unix_seconds(timestamp)
