"""Base entity for Kaadas locks."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .hub import DeviceState, KaadasConfigEntry, KaadasHub


class KaadasEntity(Entity):
    """An entity belonging to one Kaadas lock."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: KaadasHub, device_id: str, key: str) -> None:
        self._hub = hub
        self._device_id = device_id
        self._attr_translation_key = key
        self._attr_unique_id = f"{device_id}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device_id)})
        # The hub updates this object in place. Holding it keeps the entity
        # working while Home Assistant removes it after the device was deleted.
        self._device_state = hub.devices[device_id]

    @property
    def device_state(self) -> DeviceState:
        """Return the lock's current state."""
        return self._device_state

    @property
    def available(self) -> bool:
        """Return whether the cloud connection is up."""
        return self._hub.connected

    async def async_added_to_hass(self) -> None:
        """Subscribe to state and connection changes."""
        await super().async_added_to_hass()
        for signal in (
            self._hub.signal_connection,
            self._hub.signal_device_update(self._device_id),
        ):
            self.async_on_remove(
                async_dispatcher_connect(self.hass, signal, self.async_write_ha_state)
            )


def async_setup_device_entities(
    hass: HomeAssistant,
    entry: KaadasConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    entities_for_device: Callable[[KaadasHub, str], Iterable[Entity]],
) -> None:
    """Add entities for every known lock and for each lock discovered later."""
    hub = entry.runtime_data

    @callback
    def add_device(device_id: str) -> None:
        async_add_entities(entities_for_device(hub, device_id))

    for device_id in hub.devices:
        add_device(device_id)
    entry.async_on_unload(
        async_dispatcher_connect(hass, hub.signal_new_device, add_device)
    )
