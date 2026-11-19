"""Config flow for the Orkli Termowifi integration."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import network
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import CONF_IP_ADDRESS, CONF_PORT
from homeassistant.helpers import selector
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from .const import DOMAIN
from .discovery import DEFAULT_PORT, async_probe, async_scan, scan_hosts

_LOGGER = logging.getLogger(__name__)

TITLE = "Orkli TermoWifi"


def _manual_schema(host: str = "", port: int = DEFAULT_PORT) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_IP_ADDRESS, default=host): selector.TextSelector(),
            vol.Required(CONF_PORT, default=port): int,
        }
    )


def _is_connected(entry: ConfigEntry) -> bool:
    """Return whether the entry is loaded and connected to its device."""
    connector = getattr(entry, "runtime_data", None)
    return entry.state is ConfigEntryState.LOADED and bool(
        getattr(connector, "connected", False)
    )


class OrkliTermowifiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Orkli."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._scan_task: asyncio.Task[list[str]] | None = None
        self._found: list[str] = []
        self._host: str | None = None
        self._failed_manual: tuple[str, int] | None = None

    def _configured_hosts(self) -> set[str]:
        return {
            entry.data[CONF_IP_ADDRESS]
            for entry in self._async_current_entries(include_ignore=False)
            if CONF_IP_ADDRESS in entry.data
        }

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between scanning the network and entering the address."""
        return self.async_show_menu(step_id="user", menu_options=["scan", "manual"])

    async def async_step_scan(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Scan the local networks of Home Assistant."""
        if self._scan_task is None:
            self._scan_task = self.hass.async_create_task(self._async_scan())
        if not self._scan_task.done():
            return self.async_show_progress(
                step_id="scan",
                progress_action="scan",
                progress_task=self._scan_task,
            )
        try:
            self._found = self._scan_task.result()
        except (Exception, asyncio.CancelledError):
            _LOGGER.exception("Unexpected error scanning for TermoWifi devices")
            self._found = []
        self._scan_task = None
        return self.async_show_progress_done(next_step_id="pick")

    async def _async_scan(self) -> list[str]:
        """Return the TermoWifi devices found that are not configured yet."""
        interfaces = [
            ipaddress.IPv4Interface(f"{ip['address']}/{ip['network_prefix']}")
            for adapter in await network.async_get_adapters(self.hass)
            if adapter["enabled"]
            for ip in adapter["ipv4"]
        ]
        # Skip the configured devices: they already have a client connected
        configured = self._configured_hosts()
        hosts = [host for host in scan_hosts(interfaces) if str(host) not in configured]
        return await async_scan(hosts)

    async def async_step_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick one of the devices found by the scan."""
        # When the scan ends at once, HA passes the menu choice to this step
        if user_input is not None and CONF_IP_ADDRESS in user_input:
            host = user_input[CONF_IP_ADDRESS]
            self._async_abort_entries_match({CONF_IP_ADDRESS: host})
            return self._async_create(host, DEFAULT_PORT)

        if not self._found:
            return self.async_show_form(
                step_id="manual",
                data_schema=_manual_schema(),
                errors={"base": "no_devices_found"},
            )

        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_IP_ADDRESS, default=self._found[0]
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=self._found)
                    ),
                }
            ),
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enter the address of the device.

        When the check fails, submitting the same address again adds it anyway:
        the device may be busy or offline for a moment.
        """
        if user_input is None:
            return self.async_show_form(step_id="manual", data_schema=_manual_schema())

        host = user_input[CONF_IP_ADDRESS].strip()
        port = user_input[CONF_PORT]
        self._async_abort_entries_match({CONF_IP_ADDRESS: host})
        if self._failed_manual == (host, port):
            _LOGGER.warning("Adding %s:%s without a TermoWifi answer", host, port)
            return self._async_create(host, port)

        try:
            is_termowifi = await async_probe(host, port)
        except (OSError, TimeoutError) as err:
            _LOGGER.debug("Cannot connect to %s:%s: %s", host, port, err)
            error = "cannot_connect"
        else:
            if is_termowifi:
                return self._async_create(host, port)
            error = "not_termowifi"

        self._failed_manual = (host, port)
        return self.async_show_form(
            step_id="manual",
            data_schema=_manual_schema(host, port),
            errors={"base": error},
        )

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """Handle a device found by DHCP.

        The MAC prefix is shared by every Espressif device, so the device is
        only offered when it answers like a TermoWifi.
        """
        host = discovery_info.ip
        mac = format_mac(discovery_info.macaddress)
        await self.async_set_unique_id(mac)
        # Follows the IP address of a device already configured
        self._abort_if_unique_id_configured(updates={CONF_IP_ADDRESS: host})

        # Entries set up by hand have no unique id: adopt the MAC
        legacy = [
            entry
            for entry in self._async_current_entries(include_ignore=False)
            if entry.unique_id is None
        ]
        for entry in legacy:
            if entry.data.get(CONF_IP_ADDRESS) == host:
                self.hass.config_entries.async_update_entry(entry, unique_id=mac)
                return self.async_abort(reason="already_configured")
        self._async_abort_entries_match({CONF_IP_ADDRESS: host})

        try:
            is_termowifi = await async_probe(host, DEFAULT_PORT)
        except (OSError, TimeoutError):
            return self.async_abort(reason="cannot_connect")
        if not is_termowifi:
            return self.async_abort(reason="not_termowifi")

        # An entry set up by hand that lost its device probably had this one
        # before its IP address changed
        disconnected = [entry for entry in legacy if not _is_connected(entry)]
        if len(disconnected) == 1:
            entry = disconnected[0]
            _LOGGER.info(
                "Moving %s from %s to %s (%s)",
                entry.title,
                entry.data.get(CONF_IP_ADDRESS),
                host,
                mac,
            )
            self.hass.config_entries.async_update_entry(
                entry, unique_id=mac, data={**entry.data, CONF_IP_ADDRESS: host}
            )
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_abort(reason="already_configured")

        self._host = host
        self.context["title_placeholders"] = {"host": host}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the setup of a discovered device."""
        assert self._host is not None
        if user_input is not None:
            # It may have been added by hand while waiting for confirmation
            self._async_abort_entries_match({CONF_IP_ADDRESS: self._host})
            return self._async_create(self._host, DEFAULT_PORT)

        self._set_confirm_only()
        return self.async_show_form(
            step_id="discovery_confirm",
            description_placeholders={"host": self._host},
        )

    def _async_create(self, host: str, port: int) -> ConfigFlowResult:
        return self.async_create_entry(
            title=f"{TITLE} ({host})", data={CONF_IP_ADDRESS: host, CONF_PORT: port}
        )
