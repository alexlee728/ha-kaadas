"""Sensors for Kaadas locks."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import KaadasEntity, async_setup_device_entities
from .hub import KaadasConfigEntry, KaadasHub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KaadasConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Kaadas sensors."""

    def entities_for_device(hub: KaadasHub, device_id: str) -> list[SensorEntity]:
        return [KaadasSignalStrengthSensor(hub, device_id)]

    async_setup_device_entities(hass, entry, async_add_entities, entities_for_device)


class KaadasSignalStrengthSensor(KaadasEntity, SensorEntity):
    """Wi-Fi signal strength, updated whenever the lock wakes up."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, hub: KaadasHub, device_id: str) -> None:
        super().__init__(hub, device_id, "rssi")

    @property
    def native_value(self) -> int | None:
        """Return the received signal strength."""
        return self.device_state.rssi
