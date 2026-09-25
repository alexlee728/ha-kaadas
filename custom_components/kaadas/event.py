"""Event entities for Kaadas locks."""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import KaadasEntity, async_setup_device_entities
from .hub import KaadasConfigEntry, KaadasHub
from .protocol import (
    UNLOCK_METHOD_OTHER,
    UNLOCK_METHODS,
    KaadasMessage,
    LockAlarm,
    LockRecord,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KaadasConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Kaadas event entities."""

    def entities_for_device(hub: KaadasHub, device_id: str) -> list[EventEntity]:
        return [
            KaadasUnlockEvent(hub, device_id),
            KaadasDoorbellEvent(hub, device_id),
            KaadasAlarmEvent(hub, device_id),
        ]

    async_setup_device_entities(hass, entry, async_add_entities, entities_for_device)


class KaadasEventEntity(KaadasEntity, EventEntity):
    """An event entity fed by the lock's records and alarms."""

    async def async_added_to_hass(self) -> None:
        """Subscribe to lock events."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                self._hub.signal_device_event(self._device_id),
                self._handle_lock_event,
            )
        )

    @callback
    def _handle_lock_event(self, message: KaadasMessage) -> None:
        raise NotImplementedError


class KaadasUnlockEvent(KaadasEventEntity):
    """Fires when the lock is unlocked; the event type is the unlock method."""

    _attr_event_types = [*UNLOCK_METHODS.values(), UNLOCK_METHOD_OTHER]

    def __init__(self, hub: KaadasHub, device_id: str) -> None:
        super().__init__(hub, device_id, "unlock")

    @callback
    def _handle_lock_event(self, message: KaadasMessage) -> None:
        if not isinstance(message, LockRecord) or not message.is_unlock:
            return
        self._trigger_event(
            message.unlock_method,
            {
                "user": message.user_name,
                "user_id": message.user_id,
                "credential": message.credential_name,
                "event_source": message.event_source,
            },
        )
        self.async_write_ha_state()


class KaadasDoorbellEvent(KaadasEventEntity):
    """Fires when the doorbell button on the lock is pressed."""

    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = ["ring"]

    def __init__(self, hub: KaadasHub, device_id: str) -> None:
        super().__init__(hub, device_id, "doorbell")

    @callback
    def _handle_lock_event(self, message: KaadasMessage) -> None:
        if not isinstance(message, LockAlarm) or not message.is_doorbell:
            return
        self._trigger_event("ring")
        self.async_write_ha_state()


class KaadasAlarmEvent(KaadasEventEntity):
    """Fires when the lock raises an alarm other than the doorbell."""

    _attr_event_types = ["alarm"]

    def __init__(self, hub: KaadasHub, device_id: str) -> None:
        super().__init__(hub, device_id, "alarm")

    @callback
    def _handle_lock_event(self, message: KaadasMessage) -> None:
        if not isinstance(message, LockAlarm) or message.is_doorbell:
            return
        self._trigger_event("alarm", {"alarm_code": message.alarm_code})
        self.async_write_ha_state()
