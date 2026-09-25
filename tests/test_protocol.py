"""Tests for Kaadas push message parsing."""

from __future__ import annotations

from datetime import UTC, datetime

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
