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
    """Alarm raised by the lock, including doorbell presses."""

    alarm_code: int | None

    @property
    def is_doorbell(self) -> bool:
        """Return whether the alarm is a doorbell press."""
        return self.alarm_code == ALARM_CODE_DOORBELL


def decode_payload(raw: bytes) -> Any | None:
    """Decode a raw MQTT payload as JSON, returning None when it is not."""
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def parse_message(payload: Any) -> KaadasMessage | None:
    """Parse a decoded payload into a lock message.

    Returns None for anything that is not a recognised lock event.
    """
    if not isinstance(payload, dict):
        return None
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
