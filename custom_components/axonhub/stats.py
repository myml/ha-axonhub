"""Instance wide AxonHub statistics, shown on the integration's hub device.

AxonHub's admin GraphQL exposes two instance level aggregates:

* ``dashboardOverview`` – request counters. ``totalRequests`` and
  ``failedRequests`` count rows of the ``requests`` table (AxonHub's process
  tracking, failures included), while ``requestStats`` counts ``usage_logs`` rows
  (successful results only — the numbers the AxonHub dashboard itself charts).
  The two therefore answer different questions, which is why the entity names
  keep them apart: ``Total requests`` is every request AxonHub tried to serve,
  ``Requests today`` is the requests that produced a result.
* ``tokenStats`` – input, output and cached token sums for today, this week,
  this month and all time. AxonHub answers the all time part from a
  stale-while-revalidate cache, so polling the whole field stays cheap.

Both fields need the ``read:dashboard`` scope. An account that may only read
channels gets no statistics at all; the coordinator reports that as "statistics
unavailable" instead of failing the integration.

Token bucket semantics (see AxonHub's ``docs/en/guides/cost-tracking.md``):
``prompt_tokens`` **excludes** the cached prompt tokens, so a window's total is
``input + output + cached`` and the cache hit rate is ``cached / (input +
cached)``. Cache *writes* live in separate columns that ``tokenStats`` does not
expose, so they are not part of any number here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class TokenCounts:
    """Token usage of one time window."""

    input: int = 0
    """Prompt tokens sent to the model, excluding the cached part."""

    output: int = 0
    """Completion tokens produced by the model."""

    cached: int = 0
    """Prompt tokens served from the provider's prompt cache."""

    @property
    def total(self) -> int:
        """Return the window's total token count.

        Cached tokens are added on top because AxonHub keeps them out of
        ``prompt_tokens``.
        """
        return self.input + self.output + self.cached

    @property
    def cache_hit_rate(self) -> float | None:
        """Return the share of prompt side tokens served from the cache.

        ``None`` when the window has no prompt tokens at all, so an idle window
        reports "no data" instead of a misleading 0%.
        """
        prompt = self.input + self.cached
        if prompt <= 0:
            return None
        return round(self.cached * 100 / prompt, 2)


@dataclass(slots=True)
class RequestCounts:
    """Request counters of the AxonHub instance."""

    total: int = 0
    """Every request AxonHub tracked, failed ones included."""

    failed: int = 0
    """Requests that ended in a failure status."""

    today: int = 0
    this_week: int = 0
    last_week: int = 0
    this_month: int = 0
    average_response_time: float | None = None
    """AxonHub's average response time; currently always null upstream."""

    @property
    def success_rate(self) -> float | None:
        """Return the share of tracked requests that did not fail.

        ``None`` while AxonHub has not tracked a single request yet.
        """
        if self.total <= 0:
            return None
        return round((self.total - self.failed) * 100 / self.total, 2)


@dataclass(slots=True)
class AxonHubStats:
    """The instance wide aggregates displayed on the hub device."""

    requests: RequestCounts
    tokens_today: TokenCounts
    tokens_this_week: TokenCounts
    tokens_this_month: TokenCounts
    tokens_all_time: TokenCounts


def build_stats(payload: dict[str, Any]) -> AxonHubStats | None:
    """Build :class:`AxonHubStats` from a dashboard GraphQL response.

    GraphQL answers with partial data when a single root field fails, so each
    aggregate is read independently and missing pieces fall back to zero.
    ``None`` means neither aggregate resolved at all — typically an account
    without the ``read:dashboard`` scope, or an AxonHub build that predates the
    dashboard API.
    """
    overview = payload.get("dashboardOverview")
    tokens = payload.get("tokenStats")

    if not isinstance(overview, dict) and not isinstance(tokens, dict):
        return None

    return AxonHubStats(
        requests=_build_requests(overview),
        tokens_today=_build_tokens(tokens, "Today"),
        tokens_this_week=_build_tokens(tokens, "ThisWeek"),
        tokens_this_month=_build_tokens(tokens, "ThisMonth"),
        tokens_all_time=_build_tokens(tokens, "AllTime"),
    )


def _build_requests(overview: Any) -> RequestCounts:
    """Normalize the ``dashboardOverview`` field."""
    if not isinstance(overview, dict):
        return RequestCounts()

    request_stats = overview.get("requestStats")
    if not isinstance(request_stats, dict):
        request_stats = {}

    return RequestCounts(
        total=_as_int(overview.get("totalRequests")),
        failed=_as_int(overview.get("failedRequests")),
        today=_as_int(request_stats.get("requestsToday")),
        this_week=_as_int(request_stats.get("requestsThisWeek")),
        last_week=_as_int(request_stats.get("requestsLastWeek")),
        this_month=_as_int(request_stats.get("requestsThisMonth")),
        average_response_time=_as_float(overview.get("averageResponseTime")),
    )


def _build_tokens(tokens: Any, window: str) -> TokenCounts:
    """Normalize one window of the ``tokenStats`` field."""
    if not isinstance(tokens, dict):
        return TokenCounts()

    return TokenCounts(
        input=_as_int(tokens.get(f"totalInputTokens{window}")),
        output=_as_int(tokens.get(f"totalOutputTokens{window}")),
        cached=_as_int(tokens.get(f"totalCachedTokens{window}")),
    )


def _as_int(value: Any) -> int:
    """Read an integer counter, treating anything unusable as zero."""
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float | None:
    """Read an optional float, returning ``None`` when it is absent."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
