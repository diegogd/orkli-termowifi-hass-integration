"""Config flow for the Orkli Termowifi integration."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import network
from homeassistant.config_entries import (
    SOURCE_IGNORE,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import CONF_IP_ADDRESS, CONF_PORT
from homeassistant.data_entry_flow import AbortFlow
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

    def _entry_for_host(
        self, host: str, exclude: ConfigEntry | None = None
    ) -> ConfigEntry | None:
        """Return the entry configured with this address, if any."""
        for entry in self._async_current_entries(include_ignore=False):
            if entry is not exclude and entry.data.get(CONF_IP_ADDRESS) == host:
                return entry
        return None

    def _async_abort_if_configured(
        self, host: str, exclude: ConfigEntry | None = None
    ) -> None:
        """Abort when the address belongs to an entry.

        A disabled or disconnected entry gets its own message: the device is
        still found, and the fix is to enable or reconfigure that entry.
        """
        if (entry := self._entry_for_host(host, exclude)) is None:
            return
        if _is_connected(entry):
            raise AbortFlow("already_configured")
        raise AbortFlow(
            "already_configured_inactive",
            description_placeholders={"host": host, "title": entry.title},
        )

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
        # Skip the connected devices: their entry already has a client. The
        # disabled or disconnected ones are scanned to tell the user about them.
        connected = {
            entry.data.get(CONF_IP_ADDRESS)
            for entry in self._async_current_entries(include_ignore=False)
            if _is_connected(entry)
        }
        hosts = [host for host in scan_hosts(interfaces) if str(host) not in connected]
        return await async_scan(hosts)

    async def async_step_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick one of the devices found by the scan."""
        # When the scan ends at once, HA passes the menu choice to this step
        if user_input is not None and CONF_IP_ADDRESS in user_input:
            host = user_input[CONF_IP_ADDRESS]
            self._async_abort_if_configured(host)
            return self._async_create(host, DEFAULT_PORT)

        if not self._found:
            return self.async_show_form(
                step_id="manual",
                data_schema=_manual_schema(),
                errors={"base": "no_devices_found"},
            )

        # Only devices of existing entries: explain how to use that entry
        if all(self._entry_for_host(host) for host in self._found):
            self._async_abort_if_configured(self._found[0])

        options = []
        for host in self._found:
            entry = self._entry_for_host(host)
            label = host if entry is None else f"{host} ({entry.title})"
            options.append(selector.SelectOptionDict(value=host, label=label))

        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_IP_ADDRESS, default=self._found[0]
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options)
                    ),
                }
            ),
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enter the address of the device."""
        if user_input is None:
            return self.async_show_form(step_id="manual", data_schema=_manual_schema())

        host = user_input[CONF_IP_ADDRESS].strip()
        port = user_input[CONF_PORT]
        self._async_abort_if_configured(host)
        if (error := await self._async_check(host, port)) is None:
            return self._async_create(host, port)
        return self.async_show_form(
            step_id="manual",
            data_schema=_manual_schema(host, port),
            errors={"base": error},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the address of an entry, keeping its entities."""
        entry = self._get_reconfigure_entry()
        if user_input is None:
            return self.async_show_form(
                step_id="reconfigure",
                data_schema=_manual_schema(
                    entry.data[CONF_IP_ADDRESS],
                    entry.data.get(CONF_PORT, DEFAULT_PORT),
                ),
            )

        host = user_input[CONF_IP_ADDRESS].strip()
        port = user_input[CONF_PORT]
        self._async_abort_if_configured(host, exclude=entry)
        if (error := await self._async_check(host, port)) is None:
            return self.async_update_reload_and_abort(
                entry, data_updates={CONF_IP_ADDRESS: host, CONF_PORT: port}
            )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_manual_schema(host, port),
            errors={"base": error},
        )

    async def _async_check(self, host: str, port: int) -> str | None:
        """Return the error of an address entered by hand, or None if valid.

        When the check fails, submitting the same address again accepts it:
        the device may be busy or offline for a moment.
        """
        if self._failed_manual == (host, port):
            _LOGGER.warning("Using %s:%s without a TermoWifi answer", host, port)
            return None

        try:
            is_termowifi = await async_probe(host, port)
        except (OSError, TimeoutError) as err:
            _LOGGER.debug("Cannot connect to %s:%s: %s", host, port, err)
            error = "cannot_connect"
        else:
            if is_termowifi:
                return None
            error = "not_termowifi"

        self._failed_manual = (host, port)
        return error

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
        # Follows the IP address of a device already configured. HA only
        # reloads loaded entries on update, so an entry still connecting to
        # the old address would keep it: always schedule the reload.
        for entry in self._async_current_entries(include_ignore=True):
            if entry.unique_id != mac:
                continue
            if (
                entry.source != SOURCE_IGNORE
                and entry.data.get(CONF_IP_ADDRESS) != host
            ):
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_IP_ADDRESS: host}
                )
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_abort(reason="already_configured")

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
            self._async_abort_if_configured(self._host)
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
