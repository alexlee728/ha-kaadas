"""Fixtures for Kaadas Smart tests."""

from __future__ import annotations

from collections.abc import Callable, Generator
from datetime import datetime
import json
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kaadas.const import (
    CONF_ACCOUNT,
    CONF_REGION,
    CONF_TOKEN,
    CONF_UID,
    DOMAIN,
)

# Message layouts follow captured Kaadas P30 Pro Max traffic; all values are
# made up.
UID = "0123456789abcdef01234567"
TOKEN = "session-token"
ACCOUNT = "ha@example.com"
WIFI_ID = "WF0000000001"
LOCK_BODY_ID = "LB0000000000000000000001"
LOCK_NAME = "Front door"
WIFI_SSID = "home-wifi"
WIFI_BSSID = "00:11:22:33:44:55"
LOCK_MAC = "66:77:88:99:aa:bb"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load the integration from custom_components."""


class FakeMqttClient:
    """Stand-in for KaadasMqttClient driven directly by tests."""

    connect_error: Exception | None = None
    # Messages the broker replays right after accepting the session.
    replay_on_connect: tuple[Any, ...] = ()

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        host: str,
        uid: str,
        token: str,
        on_message: Callable[[str, bytes], None],
        on_connection_change: Callable[[bool], None],
        on_auth_failure: Callable[[], None],
    ) -> None:
        self.host = host
        self.uid = uid
        self.token = token
        self.on_message = on_message
        self.on_connection_change = on_connection_change
        self.on_auth_failure = on_auth_failure
        self.connected = False
        self.disconnect_calls = 0

    async def async_connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.set_connected(True)
        for payload in self.replay_on_connect:
            self.push(payload)

    async def async_disconnect(self) -> None:
        self.disconnect_calls += 1
        if self.connected:
            self.set_connected(False)

    def set_connected(self, connected: bool) -> None:
        self.connected = connected
        self.on_connection_change(connected)

    def push(self, payload: Any) -> None:
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.on_message(f"/kiot/{self.uid}/app/down", raw)


class FakeClientFactory:
    """Records the fake clients created by the hub."""

    def __init__(self) -> None:
        self.instances: list[FakeMqttClient] = []
        self.connect_error: Exception | None = None
        self.replay_on_connect: tuple[Any, ...] = ()

    def __call__(self, *args: Any, **kwargs: Any) -> FakeMqttClient:
        client = FakeMqttClient(*args, **kwargs)
        client.connect_error = self.connect_error
        client.replay_on_connect = self.replay_on_connect
        self.instances.append(client)
        return client

    @property
    def client(self) -> FakeMqttClient:
        return self.instances[-1]


@pytest.fixture
def mqtt_client() -> Generator[FakeClientFactory]:
    """Replace the MQTT client with a fake."""
    factory = FakeClientFactory()
    with patch("custom_components.kaadas.hub.KaadasMqttClient", factory):
        yield factory


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Return a config entry at the current version."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=ACCOUNT,
        unique_id=UID,
        version=1,
        data={
            CONF_ACCOUNT: ACCOUNT,
            CONF_REGION: "cn",
            CONF_UID: UID,
            CONF_TOKEN: TOKEN,
        },
    )


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mqtt_client: FakeClientFactory,
) -> FakeMqttClient:
    """Set up the integration and return the connected fake client."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return mqtt_client.client


def lock_event(
    event_type: str,
    params: Any,
    *,
    timestamp: datetime | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Build a push message as sent by the Kaadas cloud."""
    return {
        "msgtype": "event",
        "func": "wfevent",
        "msgId": 1,
        "devtype": "kdswflock",
        "eventtype": event_type,
        "wfId": WIFI_ID,
        "lockId": WIFI_ID,
        "uid": UID,
        "timestamp": int((timestamp or dt_util.utcnow()).timestamp()),
        "eventparams": params,
        **fields,
    }


def lock_info(**params: Any) -> dict[str, Any]:
    """Build a lockInf status report.

    Status reports carry the lock body ID as lockId, a string timestamp and
    string RSSI, and no lock nickname.
    """
    message = lock_event(
        "lockInf",
        {
            "sn": WIFI_ID,
            "lockModel": "K3AWE",
            "devErrCode": "00000000",
            "lockType": 2,
            "wifiSSID": WIFI_SSID,
            "RSSI": "-60",
            "wifiStrength": 100,
            "BISSID": WIFI_BSSID,
            "mac": LOCK_MAC,
            "MQTTversion": "1.1.8",
            **params,
        },
        lockId=LOCK_BODY_ID,
        msgId=27667,
        mqttPayloadVersion="2.0.4",
    )
    message["timestamp"] = str(message["timestamp"])
    return message


def record(
    event_code: int,
    event_source: int,
    *,
    timestamp: datetime | None = None,
    user_id: int = 0,
    **fields: Any,
) -> dict[str, Any]:
    """Build a lock or unlock record."""
    return lock_event(
        "record",
        {
            "eventCode": event_code,
            "appID": 0,
            "eventSource": event_source,
            "eventType": 1,
            "userID": user_id,
        },
        timestamp=timestamp,
        **fields,
    )


def alarm(alarm_code: int, *, timestamp: datetime | None = None) -> dict[str, Any]:
    """Build an alarm message."""
    return lock_event(
        "alarm",
        {"alarmCode": alarm_code, "txEventId": ""},
        timestamp=timestamp,
        msgId="1",
    )
