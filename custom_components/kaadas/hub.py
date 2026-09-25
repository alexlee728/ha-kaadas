"""Device state hub for a Kaadas account."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .client import KaadasMqttClient
from .const import (
    CONF_REGION,
    CONF_TOKEN,
    CONF_UID,
    DOMAIN,
    ENDPOINTS,
    MANUFACTURER,
    MAX_EVENT_AGE,
    RECENT_MESSAGE_LIMIT,
    REPLAY_WINDOW,
    Region,
)
from .protocol import (
    KaadasMessage,
    LockAlarm,
    LockInfo,
    LockRecord,
    decode_payload,
    parse_message,
)

_LOGGER = logging.getLogger(__name__)

type KaadasConfigEntry = ConfigEntry[KaadasHub]


@dataclass(slots=True)
class DeviceState:
    """Latest known state of one lock."""

    device_id: str
    serial_number: str | None = None
    name: str | None = None
    model: str | None = None
    is_locked: bool | None = None
    lock_changed_at: datetime | None = None
    error_code: str | None = None
    rssi: int | None = None


@dataclass(frozen=True, slots=True)
class ReceivedMessage:
    """Raw message kept for the diagnostics download."""

    received_at: datetime
    topic: str
    payload: Any


class KaadasHub:
    """Track the locks of one Kaadas account and notify entities of changes.

    Entities subscribe through dispatcher signals:
    - signal_new_device: a lock was seen for the first time
    - signal_connection: the cloud connection went up or down
    - signal_device_update(device_id): the lock's state changed
    - signal_device_event(device_id): the lock reported a record or alarm

    The broker keeps a persistent session and replays queued messages right
    after each connect. Messages arriving within REPLAY_WINDOW of a connect
    may be replays: they are applied only if newer than the known state, and
    fire events only if younger than MAX_EVENT_AGE. Each replayed message
    extends the window, so a slowly drained queue is still recognised. Later
    messages are live and apply in arrival order regardless of the lock clock.
    """

    def __init__(self, hass: HomeAssistant, entry: KaadasConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.devices: dict[str, DeviceState] = {}
        # Cleared while unloading so no entities are added to closing platforms.
        self.discovery_enabled = True
        self.recent_messages: deque[ReceivedMessage] = deque(
            maxlen=RECENT_MESSAGE_LIMIT
        )
        self._device_id_by_lock_id: dict[str, str] = {}
        self._replay_until: datetime | None = None
        # Events received before the event entities subscribed.
        self._pending_events: list[tuple[str, KaadasMessage]] | None = []
        self._client = KaadasMqttClient(
            hass,
            host=ENDPOINTS[Region(entry.data[CONF_REGION])].mqtt_host,
            uid=entry.data[CONF_UID],
            token=entry.data[CONF_TOKEN],
            on_message=self._handle_message,
            on_connection_change=self._handle_connection_change,
            on_auth_failure=self._handle_auth_failure,
        )

    @property
    def connected(self) -> bool:
        """Return whether the cloud connection is up."""
        return self._client.connected

    @property
    def signal_new_device(self) -> str:
        """Return the signal sent when a lock is seen for the first time."""
        return f"{DOMAIN}_{self.entry.entry_id}_new_device"

    @property
    def signal_connection(self) -> str:
        """Return the signal sent when the cloud connection changes."""
        return f"{DOMAIN}_{self.entry.entry_id}_connection"

    def signal_device_update(self, device_id: str) -> str:
        """Return the signal sent when a lock's state changes."""
        return f"{DOMAIN}_{self.entry.entry_id}_update_{device_id}"

    def signal_device_event(self, device_id: str) -> str:
        """Return the signal sent when a lock reports a record or alarm."""
        return f"{DOMAIN}_{self.entry.entry_id}_event_{device_id}"

    async def async_start(self) -> None:
        """Load known locks and connect to the cloud."""
        self._load_known_devices()
        await self._client.async_connect()

    async def async_stop(self) -> None:
        """Disconnect from the cloud."""
        await self._client.async_disconnect()

    @callback
    def async_start_event_delivery(self) -> None:
        """Deliver events held back until the event entities subscribed."""
        pending, self._pending_events = self._pending_events, None
        for device_id, message in pending or ():
            self._send_event(device_id, message)

    @callback
    def async_restore_lock_state(
        self, device_id: str, is_locked: bool, changed_at: datetime | None
    ) -> None:
        """Merge the lock state saved before a restart.

        Messages replayed while connecting may already have set a state; the
        saved state wins only if it is newer.
        """
        state = self.devices.get(device_id)
        if state is None:
            return
        if state.is_locked is not None and (
            changed_at is None
            or state.lock_changed_at is None
            or changed_at <= state.lock_changed_at
        ):
            return
        state.is_locked = is_locked
        state.lock_changed_at = changed_at

    @callback
    def async_remove_device(self, device_id: str) -> None:
        """Forget a lock removed by the user."""
        self.devices.pop(device_id, None)
        for lock_id, known_id in list(self._device_id_by_lock_id.items()):
            if known_id == device_id:
                del self._device_id_by_lock_id[lock_id]

    @callback
    def _load_known_devices(self) -> None:
        """Recreate locks seen before a restart so their entities exist at once."""
        registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(registry, self.entry.entry_id):
            for domain, device_id in device.identifiers:
                if domain != DOMAIN:
                    continue
                self.devices[device_id] = DeviceState(
                    device_id=device_id,
                    serial_number=device.serial_number,
                    name=device.name,
                    model=device.model,
                )

    @callback
    def _handle_message(self, topic: str, raw: bytes) -> None:
        received_at = dt_util.utcnow()
        payload = decode_payload(raw)
        self.recent_messages.append(
            ReceivedMessage(
                received_at=received_at,
                topic=topic,
                payload=raw.decode("utf-8", "replace") if payload is None else payload,
            )
        )

        message = parse_message(payload)
        if message is None:
            return

        device_id = self._resolve_device_id(message)
        if device_id is None:
            _LOGGER.debug("Ignoring message without a known device: %s", payload)
            return

        state = self.devices.get(device_id)
        is_new = state is None
        if state is None:
            if not self.discovery_enabled:
                return
            state = self.devices[device_id] = DeviceState(device_id=device_id)

        may_be_replay = (
            self._replay_until is not None and received_at <= self._replay_until
        )
        is_replay = may_be_replay and _age(message, received_at) > MAX_EVENT_AGE
        if is_replay:
            self._replay_until = received_at + REPLAY_WINDOW
        registry_fields = (state.serial_number, state.name, state.model)
        _apply_message(state, message, received_at, may_be_replay)
        if is_new or registry_fields != (state.serial_number, state.name, state.model):
            self._update_device_registry(state)

        if is_new:
            async_dispatcher_send(self.hass, self.signal_new_device, device_id)
        else:
            async_dispatcher_send(self.hass, self.signal_device_update(device_id))

        if not isinstance(message, LockRecord | LockAlarm):
            return
        if is_replay:
            _LOGGER.debug("Not firing an event for a replayed message: %s", payload)
            return
        if self._pending_events is not None:
            self._pending_events.append((device_id, message))
        else:
            self._send_event(device_id, message)

    @callback
    def _send_event(self, device_id: str, message: KaadasMessage) -> None:
        async_dispatcher_send(self.hass, self.signal_device_event(device_id), message)

    @callback
    def _resolve_device_id(self, message: KaadasMessage) -> str | None:
        """Return the device ID, falling back to one learned from a lock ID.

        A lock reports more than one lock ID, so every one seen is remembered.
        """
        if message.device_id is not None:
            if message.lock_id is not None:
                self._device_id_by_lock_id[message.lock_id] = message.device_id
            return message.device_id
        if message.lock_id is not None:
            return self._device_id_by_lock_id.get(message.lock_id)
        return None

    @callback
    def _update_device_registry(self, state: DeviceState) -> None:
        # Status reports carry no nickname; records usually do and rename the
        # device later. A name set by the user is kept separately by HA.
        fields: dict[str, Any] = {"name": state.name or f"Kaadas {state.device_id}"}
        if state.model is not None:
            fields["model"] = state.model
        if state.serial_number is not None:
            fields["serial_number"] = state.serial_number
        dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, state.device_id)},
            manufacturer=MANUFACTURER,
            **fields,
        )

    @callback
    def _handle_connection_change(self, connected: bool) -> None:
        if connected:
            self._replay_until = dt_util.utcnow() + REPLAY_WINDOW
            self._abort_reauth()
        async_dispatcher_send(self.hass, self.signal_connection)

    @callback
    def _handle_auth_failure(self) -> None:
        # The client keeps retrying, in case the refusals were temporary.
        self.entry.async_start_reauth(self.hass)

    @callback
    def _abort_reauth(self) -> None:
        """Withdraw a sign-in request once the broker accepts the token again."""
        for flow in self.entry.async_get_active_flows(self.hass, {SOURCE_REAUTH}):
            self.hass.config_entries.flow.async_abort(flow["flow_id"])


def _apply_message(
    state: DeviceState,
    message: KaadasMessage,
    received_at: datetime,
    may_be_replay: bool,
) -> None:
    """Fold one message into the lock's state."""
    if message.lock_name is not None:
        state.name = message.lock_name

    match message:
        case LockInfo():
            if message.serial_number is not None:
                state.serial_number = message.serial_number
            if message.model is not None:
                state.model = message.model
            if message.error_code is not None:
                state.error_code = message.error_code
            if message.rssi is not None:
                state.rssi = message.rssi
        case LockRecord() if message.is_lock or message.is_unlock:
            # Timestamps have whole-second precision. Clamping to the receipt
            # time keeps a lock clock running ahead from making later records
            # look old.
            received_second = received_at.replace(microsecond=0)
            changed_at = min(message.timestamp or received_second, received_second)
            if (
                may_be_replay
                and state.lock_changed_at is not None
                and changed_at < state.lock_changed_at
            ):
                return
            state.is_locked = message.is_lock
            state.lock_changed_at = changed_at


def _age(message: KaadasMessage, received_at: datetime) -> timedelta:
    """Return how long ago the lock sent the message; zero if unknown."""
    if message.timestamp is None:
        return timedelta(0)
    return received_at - message.timestamp
