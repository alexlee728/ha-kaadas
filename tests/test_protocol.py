"""Tests for Kaadas push message parsing."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from custom_components.kaadas.protocol import (
    LockAlarm,
    LockInfo,
    LockRecord,
    decode_payload,
    parse_message,
)

from .conftest import LOCK_BODY_ID, WIFI_ID, alarm, lock_event, lock_info, record


def test_parse_lock_info() -> None:
    """A status report carries identity, model, error code and signal strength."""
    message = parse_message(lock_info())

    assert isinstance(message, LockInfo)
    assert message.device_id == WIFI_ID
    assert message.lock_id == LOCK_BODY_ID
    assert message.serial_number == WIFI_ID
    assert message.lock_name is None
    assert message.model == "K3AWE"
    assert message.error_code == "00000000"
    assert message.rssi == -60


def test_string_timestamp() -> None:
    """Status reports send the timestamp as a string."""
    message = parse_message(lock_info())

    assert isinstance(lock_info()["timestamp"], str)
    assert message.timestamp is not None


def test_parse_unlock_record() -> None:
    """An unlock record names the method and the user."""
    message = parse_message(
        record(2, 4, userNickName="Alex", pwdNickName="Right thumb", user_id=3)
    )

    assert isinstance(message, LockRecord)
    assert message.is_unlock
    assert not message.is_lock
    assert message.unlock_method == "fingerprint"
    assert message.user_name == "Alex"
    assert message.credential_name == "Right thumb"
    assert message.user_id == 3


def test_parse_lock_record() -> None:
    """Only lock records with source 0 mean the bolt was thrown.

    The captured lock record has eventSource 0 and userID 255.
    """
    assert parse_message(record(1, 0, user_id=255)).is_lock
    assert not parse_message(record(1, 3)).is_lock


def test_unknown_unlock_source_is_other() -> None:
    """Unlock sources without a known name map to "other"."""
    assert parse_message(record(2, 99)).unlock_method == "other"


def test_parse_doorbell_alarm() -> None:
    """Alarm code 96 is a doorbell press."""
    message = parse_message(alarm(96))

    assert isinstance(message, LockAlarm)
    assert message.is_doorbell
    assert not parse_message(alarm(3)).is_doorbell


def test_parse_loitering_alarm() -> None:
    """Alarm code 112 reports someone loitering at the door."""
    message = parse_message(alarm(112))

    assert isinstance(message, LockAlarm)
    assert message.is_loitering
    assert not message.is_doorbell
    assert not parse_message(alarm(96)).is_loitering


def test_timestamp_is_utc() -> None:
    """Timestamps are epoch seconds in UTC."""
    moment = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)
    assert parse_message(record(2, 4, timestamp=moment)).timestamp == moment


def test_device_id_falls_back_to_serial_number() -> None:
    """Messages without wfId use the serial number in the parameters."""
    payload = lock_event("alarm", {"alarmCode": 96, "sn": "SN1"})
    del payload["wfId"]

    assert parse_message(payload).device_id == "SN1"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "text",
        {"msgtype": "reply", "func": "wfevent"},
        {"msgtype": "event", "func": "other"},
        {"msgtype": "event", "func": "wfevent", "eventtype": "unknown"},
    ],
)
def test_ignores_unrelated_payloads(payload: object) -> None:
    """Anything that is not a lock event is ignored."""
    assert parse_message(payload) is None


def test_tolerates_malformed_fields() -> None:
    """Malformed values become None instead of raising."""
    payload = lock_event("record", "not a dict", timestamp=None)
    payload["timestamp"] = "not a number"

    message = parse_message(payload)

    assert isinstance(message, LockRecord)
    assert message.event_code is None
    assert message.timestamp is None
    assert not message.is_unlock


def test_decode_payload() -> None:
    """Invalid JSON or encoding decodes to None."""
    assert decode_payload(b'{"a": 1}') == {"a": 1}
    assert decode_payload(b"not json") is None
    assert decode_payload(b"\xff\xfe") is None


def kiot_event(
    event: str,
    args: list[dict[str, Any]],
    *,
    time: int = 1795098018,
) -> dict[str, Any]:
    """Build a kiot-protocol event message as newer locks send it."""
    return {
        "cmd": "event",
        "time": time,
        "msg_id": 16,
        "body": {"service": "lock_service", "event": event, "args": args},
        "did": "wyw6bh12w1r1z1ku760ryyy1",
    }


def test_parse_kiot_unlock() -> None:
    """A kiot-protocol e_unlock becomes an unlock record."""
    message = parse_message(
        kiot_event(
            "e_unlock",
            [
                {"name": "timestamp", "value": 1795098018, "service": "lock_service"},
                {"name": "method", "value": 10, "service": "lock_service"},
                {"name": "user_id", "value": 102, "service": "lock_service"},
            ],
        )
    )

    assert isinstance(message, LockRecord)
    assert message.is_unlock
    assert not message.is_lock
    assert message.unlock_method == "open_button"
    assert message.user_id == 102
    assert message.device_id == "wyw6bh12w1r1z1ku760ryyy1"


def test_parse_kiot_lock() -> None:
    """A kiot-protocol e_lock becomes a lock record."""
    message = parse_message(
        kiot_event(
            "e_lock",
            [
                {"name": "timestamp", "value": 1795098023, "service": "lock_service"},
                {"name": "source", "value": 255, "service": "lock_service"},
            ],
        )
    )

    assert isinstance(message, LockRecord)
    assert message.is_lock
    assert not message.is_unlock


def test_parse_kiot_battery() -> None:
    """A kiot-protocol battery_service report yields the battery level."""

    def cell(value: int) -> dict[str, Any]:
        return {"name": "n", "value": value, "service": "battery_service"}

    payload = {
        "cmd": "report",
        "time": 1795098021,
        "msg_id": 17,
        "body": {
            "properties": [
                {
                    "name": "battery",
                    "value": [[cell(1), cell(42), cell(0), cell(0)]],
                    "service": "battery_service",
                }
            ]
        },
        "did": "wyw6bh12w1r1z1ku760ryyy1",
    }

    message = parse_message(payload)

    assert isinstance(message, LockInfo)
    assert message.battery == 42
    assert message.device_id == "wyw6bh12w1r1z1ku760ryyy1"


def test_ignores_kiot_actions() -> None:
    """kiot-protocol action messages are not lock events."""
    payload = {
        "cmd": "action",
        "source": "srv:messaging",
        "time": 1795098021,
        "body": {
            "service": "lock_service",
            "action": "a_get_records_data",
            "status": 200,
        },
        "did": "wyw6bh12w1r1z1ku760ryyy1",
    }

    assert parse_message(payload) is None
