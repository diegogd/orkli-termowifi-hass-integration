"""Sensor platform for Orkli Termowifi integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_IP_ADDRESS,
    CONF_PORT,
    EVENT_HOMEASSISTANT_STOP,
    PRECISION_HALVES,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .termowifi_connector import (
    OperationState,
    Room as TermowifiRoom,
    State,
    TermowifiConnector,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Initialize Orkli Termowifi config entry."""

    orkli_handler = TermowifiConnector(
        config_entry.data[CONF_IP_ADDRESS],
        config_entry.data[CONF_PORT],
        hass=hass,
    )
    # Closes the connection when HA stops
    hass.bus.async_listen_once(
        EVENT_HOMEASSISTANT_STOP,
        lambda _event: hass.loop.call_soon_threadsafe(
            hass.async_create_task, orkli_handler.async_close()
        ),
    )

    _LOGGER.debug("Initializing Orkli Termowifi connector")
    await orkli_handler.async_initialize()
    _LOGGER.debug("Orkli Termowifi connector initialized")

    async def _async_add_room(room: TermowifiRoom) -> None:
        """Add a new room entity (runs on the event loop)."""
        entity = orkli_termowifiSensorEntity(
            unique_id=f"{config_entry.entry_id}_{room.id}",
            room=room,
            connector=orkli_handler,
        )
        async_add_entities([entity])
        await entity.async_update()

    async_dispatcher_connect(
        hass,
        f"{DOMAIN}_new_room",
        lambda room: hass.loop.call_soon_threadsafe(
            hass.async_create_task, _async_add_room(room)
        ),
    )


class orkli_termowifiSensorEntity(ClimateEntity):
    """orkli_termowifi Sensor."""

    _attr_hvac_mode = HVACMode.OFF
    _attr_hvac_modes = [HVACMode.COOL, HVACMode.HEAT, HVACMode.OFF]
    _attr_max_temp = 35
    _attr_min_temp = 15
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )
    _attr_target_temperature_step = PRECISION_HALVES
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        unique_id: str,
        room: TermowifiRoom,
        connector: TermowifiConnector,
    ) -> None:
        """Initialize orkli_termowifi Sensor."""
        super().__init__()
        self._wrapped_entity_id = room.id
        self._attr_name = room.name
        self._attr_unique_id = unique_id
        self._room: TermowifiRoom = room
        self._connector: TermowifiConnector = connector
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, room.id)},
            name=f"{DOMAIN}_{room.id}",
            manufacturer="Orkli",
        )
        self._room.updated_callback = self.refresh_state

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set hvac mode."""
        if hvac_mode == HVACMode.OFF:
            await self._connector.set_state(
                room_id=self._room.id,
                state=State.OFF,
            )
        else:
            await self._connector.set_state(
                room_id=self._room.id,
                state=State.ON,
            )
            await self._connector.set_operation_mode(
                room_id=self._room.id,
                operation_state=OperationState.HEAT
                if hvac_mode == HVACMode.HEAT
                else OperationState.COOL,
            )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return

        await self._connector.set_temperature(
            room_id=self._room.id, temperature=temperature
        )

    def refresh_state(self) -> None:
        """Refresh state of the entity."""
        self._attr_current_temperature = self._room.temperature
        self._attr_target_temperature = self._room.conf_temperature
        self._attr_current_humidity = self._room.humidity

        if self._room.state is None or self._room.state == State.OFF:
            self._attr_hvac_mode = HVACMode.OFF
        elif self._room.operation_state == OperationState.HEAT:
            self._attr_hvac_mode = HVACMode.HEAT
        elif self._room.operation_state == OperationState.COOL:
            self._attr_hvac_mode = HVACMode.COOL

        _LOGGER.debug(
            "Updated entity %s: current_temperature=%s, target_temperature=%s, hvac_mode=%s, humidity=%s",
            self._attr_name,
            self._attr_current_temperature,
            self._attr_target_temperature,
            self._attr_hvac_mode,
            self._attr_current_humidity,
        )
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Get the latest data."""
        # _LOGGER.debug("Updating entity %s", self._attr_name)

        await self._connector.update_room(self._room.id)
