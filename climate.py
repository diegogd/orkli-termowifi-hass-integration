"""Sensor platform for Orkli Termowifi integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_ENTITY_ID,
    CONF_IP_ADDRESS,
    CONF_PORT,
    PRECISION_HALVES,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .termowifi_connector import Room as TermowifiRoom, TermowifiConnector


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
    await orkli_handler.async_initialize()

    entities = [
        orkli_termowifiSensorEntity(
            unique_id=f"{config_entry.entry_id}_{room.id}",
            handler=room,
        )
        for room in orkli_handler.get_rooms()
    ]

    async_add_entities(entities, True)


class orkli_termowifiSensorEntity(ClimateEntity):
    """orkli_termowifi Sensor."""

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
        handler: TermowifiRoom,
    ) -> None:
        """Initialize orkli_termowifi Sensor."""
        super().__init__()
        self._wrapped_entity_id = handler.id
        self._attr_name = handler.name
        self._attr_unique_id = unique_id
        self._handler: TermowifiRoom = handler
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, handler.id)},
            name=f"{DOMAIN}_{handler.id}",
            manufacturer="Orkli",
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set hvac mode."""
        if hvac_mode == HVACMode.HEAT:
            temperature = max(self.min_temp, self.target_temperature or self.min_temp)
            await self._handler.set_room_target_temperature(
                self._device_id, temperature, True
            )
        elif hvac_mode == HVACMode.OFF:
            await self._handler.set_room_target_temperature(
                self._device_id, self.min_temp, False
            )
        else:
            return
        await self._handler.update()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return

        await self._handler.set_temperature(temperature)

    async def async_update(self) -> None:
        """Get the latest data."""

        await self._handler.update()
        self._attr_current_temperature = self._handler.temperature
        self._attr_target_temperature = self._handler.conf_temperature
        self._attr_hvac_mode = HVACMode.HEAT if self._handler.state else HVACMode.OFF
        self._attr_current_humidity = self._handler.humidity
