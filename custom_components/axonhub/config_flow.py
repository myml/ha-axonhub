"""Config flow for the AxonHub integration."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
import logging
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    AxonHubApiError,
    AxonHubAuthError,
    AxonHubClient,
    AxonHubConnectionError,
    normalize_base_url,
)
from .const import (
    CONF_BASE_URL,
    CONF_SCAN_INTERVAL_SECONDS,
    CONF_VERIFY_SSL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8090"


def verify_ssl_for_entry(entry: ConfigEntry) -> bool:
    """Return the effective ``verify_ssl`` setting of an entry."""
    return bool(
        entry.options.get(CONF_VERIFY_SSL, entry.data.get(CONF_VERIFY_SSL, True))
    )


def scan_interval_for_entry(entry: ConfigEntry) -> timedelta:
    """Return the effective polling interval of an entry."""
    seconds = int(
        entry.options.get(
            CONF_SCAN_INTERVAL_SECONDS, int(DEFAULT_SCAN_INTERVAL.total_seconds())
        )
    )
    return timedelta(seconds=max(MIN_SCAN_INTERVAL_SECONDS, seconds))


class AxonHubConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the AxonHub config and reauth flows."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._reauth_entry: ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the AxonHub URL and account credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            data = dict(user_input)
            try:
                data[CONF_BASE_URL] = normalize_base_url(data.get(CONF_BASE_URL, ""))
            except AxonHubConnectionError:
                errors["base"] = "invalid_url"
            else:
                errors = await self._async_validate(data)
                if not errors:
                    await self.async_set_unique_id(data[CONF_BASE_URL])
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=_entry_title(data[CONF_BASE_URL]),
                        data=data,
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=self._credentials_schema(user_input),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start the reauth flow after AxonHub rejected the credentials."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for credentials again."""
        entry = self._reauth_entry
        if entry is None:
            return self.async_abort(reason="reauth_entry_missing")

        errors: dict[str, str] = {}

        if user_input is not None:
            data = {
                **entry.data,
                CONF_EMAIL: user_input[CONF_EMAIL],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }

            errors = await self._async_validate(data)
            if not errors:
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EMAIL, default=entry.data.get(CONF_EMAIL, "")
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL)),
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"url": entry.data.get(CONF_BASE_URL, "")},
        )

    @staticmethod
    def async_get_options_flow(entry: ConfigEntry) -> AxonHubOptionsFlow:
        """Return the options flow."""
        return AxonHubOptionsFlow()

    def _credentials_schema(self, user_input: dict[str, Any] | None) -> vol.Schema:
        """Build the credentials form schema."""
        return vol.Schema(
            {
                vol.Required(
                    CONF_BASE_URL,
                    default=(user_input or {}).get(CONF_BASE_URL, DEFAULT_BASE_URL),
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
                vol.Required(
                    CONF_EMAIL, default=(user_input or {}).get(CONF_EMAIL, "")
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL)),
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Optional(
                    CONF_VERIFY_SSL,
                    default=(user_input or {}).get(CONF_VERIFY_SSL, True),
                ): BooleanSelector(),
            }
        )

    async def _async_validate(self, data: dict[str, Any]) -> dict[str, str]:
        """Sign in and read the channel list to validate the setup."""
        session = async_get_clientsession(
            self.hass, verify_ssl=bool(data.get(CONF_VERIFY_SSL, True))
        )
        client = AxonHubClient(
            session,
            data[CONF_BASE_URL],
            data[CONF_EMAIL],
            data[CONF_PASSWORD],
        )

        try:
            await client.async_sign_in()
            await client.async_get_channels()
        except AxonHubAuthError as err:
            _LOGGER.debug("AxonHub authentication failed: %s", err)
            return {"base": "invalid_auth"}
        except AxonHubConnectionError as err:
            _LOGGER.debug("AxonHub is not reachable: %s", err)
            return {"base": "cannot_connect"}
        except AxonHubApiError as err:
            _LOGGER.debug("AxonHub API error during validation: %s", err)
            if "permission" in str(err).lower():
                return {"base": "insufficient_permissions"}
            return {"base": "api_error"}

        return {}


class AxonHubOptionsFlow(OptionsFlow):
    """Options for the AxonHub integration."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage polling and TLS options."""
        entry = self.config_entry

        if user_input is not None:
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL_SECONDS,
                        default=int(
                            scan_interval_for_entry(entry).total_seconds()
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL_SECONDS,
                            max=MAX_SCAN_INTERVAL_SECONDS,
                            step=1,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_VERIFY_SSL,
                        default=verify_ssl_for_entry(entry),
                    ): BooleanSelector(),
                }
            ),
        )


def _entry_title(base_url: str) -> str:
    """Build a readable config entry title."""
    parsed = urlparse(base_url)
    return parsed.netloc or base_url
