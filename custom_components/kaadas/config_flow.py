"""Config flow for Kaadas Smart."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import LoginResult, async_login
from .const import CONF_ACCOUNT, CONF_REGION, CONF_TOKEN, CONF_UID, DOMAIN, Region
from .exceptions import KaadasAuthError, KaadasConnectionError

_LOGGER = logging.getLogger(__name__)

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCOUNT): TextSelector(),
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
        vol.Required(CONF_REGION, default=Region.CN): SelectSelector(
            SelectSelectorConfig(
                options=[region.value for region in Region],
                translation_key=CONF_REGION,
                mode=SelectSelectorMode.LIST,
            )
        ),
    }
)

STEP_REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR})


class KaadasConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Kaadas Smart."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Log in with a new account."""
        errors: dict[str, str] = {}
        if user_input is not None:
            account = user_input[CONF_ACCOUNT].strip()
            region = Region(user_input[CONF_REGION])
            result = await self._async_login(
                account, user_input[CONF_PASSWORD], region, errors
            )
            if result is not None:
                await self.async_set_unique_id(result.uid)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=account,
                    data={
                        CONF_ACCOUNT: account,
                        CONF_REGION: region.value,
                        CONF_UID: result.uid,
                        CONF_TOKEN: result.token,
                    },
                )

        # Keep the account and region after an error, but not the password.
        suggested = {
            key: value
            for key, value in (user_input or {}).items()
            if key != CONF_PASSWORD
        }
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, suggested
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after the session token was rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again and obtain a new session token."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._async_login(
                entry.data[CONF_ACCOUNT],
                user_input[CONF_PASSWORD],
                Region(entry.data[CONF_REGION]),
                errors,
            )
            if result is not None:
                await self.async_set_unique_id(result.uid)
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_TOKEN: result.token}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={"account": entry.data[CONF_ACCOUNT]},
            errors=errors,
        )

    async def _async_login(
        self,
        account: str,
        password: str,
        region: Region,
        errors: dict[str, str],
    ) -> LoginResult | None:
        """Log in, recording a form error and returning None on failure."""
        try:
            return await async_login(
                async_get_clientsession(self.hass), account, password, region
            )
        except KaadasAuthError:
            errors["base"] = "invalid_auth"
        except KaadasConnectionError:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error during login")
            errors["base"] = "unknown"
        return None
