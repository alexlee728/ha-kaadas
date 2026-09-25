"""Tests for the MQTT client's connection handling."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
import logging
import threading
from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant
from paho.mqtt.client import convert_connack_rc_to_reason_code
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode
import pytest

from custom_components.kaadas.client import (
    AUTH_FAILURE_LIMIT,
    AUTH_FAILURE_REASONS,
    KaadasMqttClient,
)
from custom_components.kaadas.exceptions import KaadasAuthError, KaadasConnectionError

from .conftest import TOKEN, UID

ACCEPTED = 0x00
NOT_AUTHORIZED = 0x87
SERVER_UNAVAILABLE = 0x88


@pytest.mark.parametrize("connack_rc", [4, 5])
def test_auth_failure_reasons_match_paho(connack_rc: int) -> None:
    """MQTT v3.1.1 CONNACK codes for bad credentials map to auth failures."""
    reason = convert_connack_rc_to_reason_code(connack_rc)
    assert reason.is_failure
    assert reason.value in AUTH_FAILURE_REASONS


class Harness:
    """A client whose paho instance is a mock."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.paho = MagicMock()
        self.messages: list[tuple[str, bytes]] = []
        self.connection: list[bool] = []
        self.auth_failures = 0
        self.client = KaadasMqttClient(
            hass,
            host="broker",
            uid=UID,
            token=TOKEN,
            on_message=lambda topic, raw: self.messages.append((topic, raw)),
            on_connection_change=self.connection.append,
            on_auth_failure=self._auth_failed,
        )

    def _auth_failed(self) -> None:
        self.auth_failures += 1

    def connack(self, value: int) -> None:
        """Simulate paho delivering a CONNACK from its network thread."""
        self.client._paho_on_connect(
            self.paho,
            None,
            MagicMock(),
            ReasonCode(PacketTypes.CONNACK, identifier=value),
            None,
        )

    def schedule_connacks(self, *values: int) -> None:
        """Deliver CONNACKs shortly after the connection attempt starts."""
        for index, value in enumerate(values, start=1):
            self.hass.loop.call_later(0.01 * index, self.connack, value)

    async def connect(self) -> None:
        self.schedule_connacks(ACCEPTED)
        await self.client.async_connect()


@pytest.fixture
def harness(hass: HomeAssistant) -> Generator[Harness]:
    """Return a client wired to a mock paho instance."""
    harness = Harness(hass)
    with patch(
        "custom_components.kaadas.client.mqtt.Client", return_value=harness.paho
    ):
        yield harness


async def test_connect_subscribes_and_reports(
    hass: HomeAssistant, harness: Harness
) -> None:
    """An accepted session subscribes to both topics."""
    await harness.connect()

    assert harness.client.connected
    assert harness.connection == [True]
    ((topics,), _) = harness.paho.subscribe.call_args
    assert {topic for topic, _ in topics} == {
        f"/{UID}/rpc/reply",
        f"/kiot/{UID}/app/down",
    }
    harness.paho.username_pw_set.assert_called_once_with(UID, TOKEN)


async def test_connect_rejected_token(hass: HomeAssistant, harness: Harness) -> None:
    """Repeated auth refusals fail the first connection and stop the client."""
    harness.schedule_connacks(*[NOT_AUTHORIZED] * AUTH_FAILURE_LIMIT)

    with pytest.raises(KaadasAuthError):
        await harness.client.async_connect()

    harness.paho.loop_stop.assert_called_once()
    assert not harness.client.connected


async def test_single_auth_refusal_is_retried(
    hass: HomeAssistant, harness: Harness
) -> None:
    """One auth refusal may be a broker hiccup and does not fail setup."""
    harness.schedule_connacks(NOT_AUTHORIZED, SERVER_UNAVAILABLE, ACCEPTED)

    await harness.client.async_connect()

    assert harness.client.connected


async def test_connect_timeout(hass: HomeAssistant, harness: Harness) -> None:
    """No CONNACK within the timeout fails with a connection error."""
    with (
        patch("custom_components.kaadas.client.MQTT_CONNECT_TIMEOUT", 0.01),
        pytest.raises(KaadasConnectionError),
    ):
        await harness.client.async_connect()

    harness.paho.loop_stop.assert_called_once()


async def test_cancelled_connect_stops_client(
    hass: HomeAssistant, harness: Harness
) -> None:
    """Cancelling setup while connecting does not leak the network thread."""
    task = hass.async_create_task(harness.client.async_connect())
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    harness.paho.loop_stop.assert_called_once()


async def test_token_rejected_after_connecting(
    hass: HomeAssistant, harness: Harness
) -> None:
    """Repeated refusals while running report the failure once and keep retrying."""
    await harness.connect()

    for _ in range(AUTH_FAILURE_LIMIT - 1):
        harness.connack(NOT_AUTHORIZED)
    await hass.async_block_till_done()
    assert harness.auth_failures == 0

    for _ in range(3):
        harness.connack(NOT_AUTHORIZED)
    await hass.async_block_till_done()

    assert harness.auth_failures == 1
    harness.paho.loop_stop.assert_not_called()

    harness.connack(ACCEPTED)
    await hass.async_block_till_done()
    assert harness.client.connected


async def test_cancel_while_starting_stops_client(
    hass: HomeAssistant, harness: Harness
) -> None:
    """Cancelling while the paho client is being created still stops it."""
    release = threading.Event()

    def slow_client(*args: object, **kwargs: object) -> MagicMock:
        release.wait(5)
        return harness.paho

    with patch("custom_components.kaadas.client.mqtt.Client", side_effect=slow_client):
        task = hass.async_create_task(harness.client.async_connect())
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    harness.paho.loop_start.assert_called_once()
    harness.paho.loop_stop.assert_called_once()


async def test_successful_connect_resets_auth_refusals(
    hass: HomeAssistant, harness: Harness
) -> None:
    """Only consecutive refusals count."""
    await harness.connect()

    for _ in range(AUTH_FAILURE_LIMIT - 1):
        harness.connack(NOT_AUTHORIZED)
    harness.connack(ACCEPTED)
    harness.connack(NOT_AUTHORIZED)
    await hass.async_block_till_done()

    assert harness.auth_failures == 0


async def test_messages_and_disconnects_reach_the_event_loop(
    hass: HomeAssistant, harness: Harness
) -> None:
    """paho thread callbacks are delivered on the event loop."""
    await harness.connect()

    message = MagicMock(topic="t", payload=b"{}")
    harness.client._paho_on_message(harness.paho, None, message)
    harness.client._paho_on_disconnect(
        harness.paho, None, MagicMock(), MagicMock(), None
    )
    await hass.async_block_till_done()

    assert harness.messages == [("t", b"{}")]
    assert harness.connection == [True, False]


async def test_refused_subscription_is_logged(
    hass: HomeAssistant, harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """A refused subscription would silently receive nothing, so it is logged."""
    await harness.connect()

    harness.client._paho_on_subscribe(
        harness.paho, None, 1, [ReasonCode(PacketTypes.SUBACK, identifier=0x80)], None
    )
    with caplog.at_level(logging.WARNING):
        await hass.async_block_till_done()

    assert "refused the event subscription" in caplog.text
