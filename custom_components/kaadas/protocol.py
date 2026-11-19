"""Decoding of Kaadas MQTT push messages.

This module has no Home Assistant dependencies so the protocol can be tested
in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from typing import Any

TOPIC_RPC_REPLY = "/{uid}/rpc/reply"
TOPIC_APP_DOWN = "/kiot/{uid}/app/down"

RECORD_CODE_LOCKED = 1
RECORD_CODE_UNLOCKED = 2
# Lock records only mean "bolt thrown" when reported with this source.
RECORD_SOURCE_LOCKED = 0

ALARM_CODE_DOORBELL = 96
# Someone has stayed in front of the door for a long time.
ALARM_CODE_LOITERING = 112

NO_ERROR_CODE = "00000000"

UNLOCK_METHODS: dict[int, str] = {
    4: "fingerprint",
    7: "face",
    8: "visitor",
    9: "mechanical_key",
    10: "open_button",
}
UNLOCK_METHOD_OTHER = "other"


@dataclass(frozen=True, slots=True, kw_only=True)
class KaadasMessage:
    """Fields common to every lock event.

    device_id is the Wi-Fi module ID (wfId), present in every observed
    message. lock_id is not a stable identifier: status reports carry the
    lock body ID while records carry the Wi-Fi module ID.
    """

    device_id: str | None
    lock_id: str | None
    lock_name: str | None
    timestamp: datetime | None


@dataclass(frozen=True, slots=True, kw_only=True)
class LockInfo(KaadasMessage):
    """Periodic status report sent when the lock wakes up."""

    serial_number: str | None
    model: str | None
    error_code: str | None
    rssi: int | None
    battery: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class LockRecord(KaadasMessage):
    """Lock or unlock operation."""

    event_code: int | None
    event_source: int | None
    user_id: int | None
    user_name: str | None
    credential_name: str | None

    @property
    def is_lock(self) -> bool:
        """Return whether the record reports the bolt being thrown."""
        return (
            self.event_code == RECORD_CODE_LOCKED
            and self.event_source == RECORD_SOURCE_LOCKED
        )

    @property
    def is_unlock(self) -> bool:
        """Return whether the record reports an unlock."""
        return self.event_code == RECORD_CODE_UNLOCKED

    @property
    def unlock_method(self) -> str:
        """Return the unlock method name, or "other" for unknown sources."""
        if self.event_source is None:
            return UNLOCK_METHOD_OTHER
        return UNLOCK_METHODS.get(self.event_source, UNLOCK_METHOD_OTHER)


@dataclass(frozen=True, slots=True, kw_only=True)
class LockAlarm(KaadasMessage):
    """Alarm raised by the lock, including doorbell presses and loitering."""

    alarm_code: int | None

    @property
    def is_doorbell(self) -> bool:
        """Return whether the alarm is a doorbell press."""
        return self.alarm_code == ALARM_CODE_DOORBELL

    @property
    def is_loitering(self) -> bool:
        """Return whether the alarm reports someone loitering at the door."""
        return self.alarm_code == ALARM_CODE_LOITERING


def decode_payload(raw: bytes) -> Any | None:
    """Decode a raw MQTT payload as JSON, returning None when it is not."""
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def parse_message(payload: Any) -> KaadasMessage | None:  # noqa: PLR0911
    """Parse a decoded payload into a lock message.

    Returns None for anything that is not a recognised lock event.
    """
    if not isinstance(payload, dict):
        return None

    # Newer locks (e.g. the Q20F Pro) use the "kiot" protocol. The messages
    # carry a "cmd" ("event"/"report"/"action") and a "did" (the Wi-Fi module
    # ID) instead of the older msgtype/func/eventtype/eventparams layout.
    if "cmd" in payload and "did" in payload:
        return _parse_kiot_message(payload)

    # Older locks (e.g. the P30 Pro Max) use the "wfevent" protocol.
    if payload.get("msgtype") != "event" or payload.get("func") != "wfevent":
        return None

    params = payload.get("eventparams")
    if not isinstance(params, dict):
        params = {}

    common: dict[str, Any] = {
        "device_id": _to_str(payload.get("wfId")) or _to_str(params.get("sn")),
        "lock_id": _to_str(payload.get("lockId")),
        "lock_name": _to_str(payload.get("lockNickname")),
        "timestamp": _to_datetime(payload.get("timestamp")),
    }

    match payload.get("eventtype"):
        case "lockInf":
            return LockInfo(
                **common,
                serial_number=_to_str(params.get("sn")),
                model=_to_str(params.get("lockModel")),
                error_code=_to_str(params.get("devErrCode")),
                rssi=_to_int(params.get("RSSI")),
            )
        case "record":
            return LockRecord(
                **common,
                event_code=_to_int(params.get("eventCode")),
                event_source=_to_int(params.get("eventSource")),
                user_id=_to_int(params.get("userID")),
                user_name=_to_str(payload.get("userNickName")),
                credential_name=_to_str(payload.get("pwdNickName")),
            )
        case "alarm":
            return LockAlarm(
                **common,
                alarm_code=_to_int(params.get("alarmCode")),
            )
    return None


def _parse_kiot_message(payload: dict[str, Any]) -> KaadasMessage | None:  # noqa: PLR0911
    """Parse a kiot-protocol payload sent by newer locks.

    Example (unlock)::

        {"cmd": "event", "time": 1795098018, "msg_id": 16,
         "body": {"service": "lock_service", "event": "e_unlock",
                  "args": [{"name": "...", "value": 1795098018},
                           {"name": "...", "value": 10},
                           {"name": "...", "value": 102}]},
         "did": "wyw6bh12w1r1z1ku760ryyy1"}

    Example (lock)::

        {"cmd": "event", "body": {"service": "lock_service", "event": "e_lock",
                                  "args": [{"name": "...", "value": 1795098023},
                                           {"name": "...", "value": 255}]},
         "did": "wyw6bh12w1r1z1ku760ryyy1"}

    For e_unlock the positional args are ``[timestamp, method, user_id]``; for
    e_lock they are ``[timestamp, ...]``.
    """
    device_id = _to_str(payload.get("did"))
    if device_id is None:
        return None

    body = payload.get("body")
    if not isinstance(body, dict):
        return None

    cmd = payload.get("cmd")

    if cmd == "report":
        # Status reports carry a list of per-service properties; the service
        # is on each property, not on the body.
        properties = body.get("properties")
        if isinstance(properties, list):
            for prop in properties:
                if isinstance(prop, dict) and prop.get("service") == "battery_service":
                    battery = _battery_level(prop)
                    if battery is not None:
                        return LockInfo(
                            device_id=device_id,
                            lock_id=None,
                            lock_name=None,
                            timestamp=_to_datetime(payload.get("time")),
                            serial_number=None,
                            model=None,
                            error_code=None,
                            rssi=None,
                            battery=battery,
                        )
        return None

    if cmd != "event" or body.get("service") != "lock_service":
        return None

    event = body.get("event")
    args = body.get("args")
    args = args if isinstance(args, list) else []
    values = [_to_int(arg.get("value")) for arg in args if isinstance(arg, dict)]

    if event == "e_unlock":
        timestamp = (
            _to_datetime(values[0]) if values else _to_datetime(payload.get("time"))
        )
        return LockRecord(
            device_id=device_id,
            lock_id=None,
            lock_name=None,
            timestamp=timestamp,
            event_code=RECORD_CODE_UNLOCKED,
            event_source=values[1] if len(values) > 1 else None,
            user_id=values[2] if len(values) > 2 else None,
            user_name=None,
            credential_name=None,
        )

    if event == "e_lock":
        timestamp = (
            _to_datetime(values[0]) if values else _to_datetime(payload.get("time"))
        )
        return LockRecord(
            device_id=device_id,
            lock_id=None,
            lock_name=None,
            timestamp=timestamp,
            event_code=RECORD_CODE_LOCKED,
            event_source=RECORD_SOURCE_LOCKED,
            user_id=None,
            user_name=None,
            credential_name=None,
        )

    return None


def _battery_level(prop: dict[str, Any]) -> int | None:
    """Extract the battery percentage from a battery_service property.

    The value is a nested list of cells, e.g.
    ``[[{value: index}, {value: electricity}, {value: charging},
    {value: cycles}]]``; the second cell holds the percentage.
    """
    value = prop.get("value")
    if not isinstance(value, list) or not value or not isinstance(value[0], list):
        return None
    cells = value[0]
    if len(cells) < 2 or not isinstance(cells[1], dict):
        return None
    return _to_int(cells[1].get("value"))


def _to_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_datetime(value: Any) -> datetime | None:
    seconds = _to_int(value)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None
