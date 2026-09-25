"""Binary sensors for Kaadas locks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.util import dt as dt_util

from .entity import KaadasEntity, async_setup_device_entities
from .hub import KaadasConfigEntry, KaadasHub
from .protocol import NO_ERROR_CODE


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KaadasConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Kaadas binary sensors."""

    def entities_for_device(hub: KaadasHub, device_id: str) -> list[BinarySensorEntity]:
        return [
            KaadasLockSensor(hub, device_id),
            KaadasProblemSensor(hub, device_id),
        ]

    async_setup_device_entities(hass, entry, async_add_entities, entities_for_device)


@dataclass(frozen=True, slots=True)
class LockStoredData(ExtraStoredData):
    """Lock state saved across restarts, independent of entity availability."""

    is_locked: bool
    changed_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "is_locked": self.is_locked,
            "changed_at": self.changed_at.isoformat() if self.changed_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LockStoredData | None:
        """Restore from a stored representation, or None if unusable."""
        is_locked = data.get("is_locked")
        if not isinstance(is_locked, bool):
            return None
        changed_at = data.get("changed_at")
        return cls(
            is_locked=is_locked,
            changed_at=dt_util.parse_datetime(changed_at) if changed_at else None,
        )


class KaadasLockSensor(KaadasEntity, BinarySensorEntity, RestoreEntity):
    """Whether the bolt is retracted; on means unlocked.

    The lock only reports changes, so the last known state is restored after a
    restart until the lock reports again.
    """

    _attr_device_class = BinarySensorDeviceClass.LOCK

    def __init__(self, hub: KaadasHub, device_id: str) -> None:
        super().__init__(hub, device_id, "lock")

    async def async_added_to_hass(self) -> None:
        """Restore the last known state into the hub."""
        await super().async_added_to_hass()
        if (extra := await self.async_get_last_extra_data()) is not None and (
            stored := LockStoredData.from_dict(extra.as_dict())
        ) is not None:
            self._hub.async_restore_lock_state(
                self._device_id, stored.is_locked, stored.changed_at
            )

    @property
    def extra_restore_state_data(self) -> LockStoredData | None:
        """Return the lock state to save across restarts."""
        state = self.device_state
        if state.is_locked is None:
            return None
        return LockStoredData(
            is_locked=state.is_locked, changed_at=state.lock_changed_at
        )

    @property
    def is_on(self) -> bool | None:
        """Return true when the lock is unlocked."""
        if (is_locked := self.device_state.is_locked) is None:
            return None
        return not is_locked


class KaadasProblemSensor(KaadasEntity, BinarySensorEntity):
    """Whether the lock reports a device error."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: KaadasHub, device_id: str) -> None:
        super().__init__(hub, device_id, "problem")

    @property
    def is_on(self) -> bool | None:
        """Return true when the lock reports an error code."""
        if (error_code := self.device_state.error_code) is None:
            return None
        return error_code != NO_ERROR_CODE

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Return the raw error code."""
        return {"error_code": self.device_state.error_code}
