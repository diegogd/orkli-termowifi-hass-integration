"""Config flow for the Orkli Termowifi integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_IP_ADDRESS, CONF_PORT
from homeassistant.helpers import selector

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_IP_ADDRESS): selector.TextSelector(),
        vol.Required(CONF_PORT, default=12345): int,
    }
)


class OrkliTermowifiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Orkli."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""

        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=CONFIG_SCHEMA,
            )

        return self.async_create_entry(
            title=user_input[CONF_IP_ADDRESS], data=user_input
        )
