"""Constants for the AxonHub integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "axonhub"

CONF_BASE_URL: Final = "base_url"
CONF_EMAIL: Final = "email"
CONF_PASSWORD: Final = "password"
CONF_VERIFY_SSL: Final = "verify_ssl"
CONF_SCAN_INTERVAL_SECONDS: Final = "scan_interval_seconds"

DEFAULT_SCAN_INTERVAL: Final = timedelta(minutes=5)
MIN_SCAN_INTERVAL_SECONDS: Final = 30
MAX_SCAN_INTERVAL_SECONDS: Final = 24 * 60 * 60

SERVICE_REFRESH_QUOTAS: Final = "refresh_quotas"
ATTR_ENTRY_ID: Final = "entry_id"

# Channels that AxonHub runs a provider quota checker for.
# Keep in sync with internal/server/biz/provider_quota.go upstream.
SUPPORTED_CHANNEL_TYPES: Final = frozenset(
    {
        "claudecode",
        "codex",
        "github_copilot",
        "nanogpt",
        "nanogpt_responses",
    }
)

# AxonHub signs JWTs that stay valid for 7 days (see internal/server/biz/auth.go).
# Re-authenticate before that instead of waiting for a 401.
TOKEN_LIFETIME: Final = timedelta(days=7)
TOKEN_REFRESH_MARGIN: Final = timedelta(days=1)

# Regular API calls (sign-in, reading cached quota status).
REQUEST_TIMEOUT: Final = 30

# A forced quota check walks every channel sequentially and calls the upstream
# provider for each one, so it can take minutes on a large instance.
CHECK_TIMEOUT: Final = 180

STATUS_OPTIONS: Final = ["available", "warning", "exhausted", "unknown"]
