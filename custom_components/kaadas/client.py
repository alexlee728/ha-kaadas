"""Kaadas cloud MQTT client.

paho-mqtt runs its network loop in a background thread. Every paho callback
only hands its arguments over to the Home Assistant event loop, so all state
changes happen on the event loop and no locking is needed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
import paho.mqtt.client as mqtt
from paho.mqtt.reasoncodes import ReasonCode

from .const import MQTT_CONNECT_TIMEOUT, MQTT_KEEPALIVE, MQTT_PORT
from .exceptions import KaadasAuthError, KaadasConnectionError
from .protocol import TOPIC_APP_DOWN, TOPIC_RPC_REPLY

_LOGGER = logging.getLogger(__name__)

# MQTT v5 reason codes; paho maps the v3.1.1 CONNACK codes 4 and 5 onto these.
BAD_USERNAME_OR_PASSWORD = 0x86
NOT_AUTHORIZED = 0x87
AUTH_FAILURE_REASONS = frozenset({BAD_USERNAME_OR_PASSWORD, NOT_AUTHORIZED})

# A broker may refuse a valid token while its auth backend is unavailable, so
# the token only counts as rejected after this many refusals in a row. Even
# then the client keeps retrying until it is stopped.
AUTH_FAILURE_LIMIT = 3

RECONNECT_MIN_DELAY = 1
RECONNECT_MAX_DELAY = 60


class KaadasMqttClient:
    """Maintain the push connection to the Kaadas cloud broker."""

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
        self._hass = hass
        self._host = host
        self._uid = uid
        self._token = token
        self._on_message = on_message
        self._on_connection_change = on_connection_change
        self._on_auth_failure = on_auth_failure
        self._client: mqtt.Client | None = None
        self._connected = False
        self._auth_refusals = 0
        self._first_connect: asyncio.Future[None] | None = None

    @property
    def connected(self) -> bool:
        """Return whether the broker connection is up."""
        return self._connected

    async def async_connect(self) -> None:
        """Connect and wait until the broker accepts or rejects the session.

        Raises KaadasAuthError when the token is rejected and
        KaadasConnectionError when the broker cannot be reached in time.
        After the first connection paho reconnects on its own.
        """
        self._first_connect = self._hass.loop.create_future()
        start = self._hass.async_add_executor_job(self._start)
        try:
            # Shielded so a cancelled setup still waits for the client to exist
            # and can stop it.
            await asyncio.shield(start)
            async with asyncio.timeout(MQTT_CONNECT_TIMEOUT):
                await self._first_connect
        except TimeoutError as err:
            await self.async_disconnect()
            raise KaadasConnectionError(
                f"No connection to {self._host} within {MQTT_CONNECT_TIMEOUT}s"
            ) from err
        except BaseException:
            await asyncio.wait([start])
            await self.async_disconnect()
            raise
        finally:
            self._first_connect = None

    async def async_disconnect(self) -> None:
        """Close the connection and stop the network thread."""
        client, self._client = self._client, None
        if client is None:
            return
        await self._hass.async_add_executor_job(_stop_client, client)
        self._set_connected(False)

    def _start(self) -> None:
        """Create the paho client and start its network thread."""
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"app:{self._uid}",
            clean_session=False,
        )
        client.username_pw_set(self._uid, self._token)
        client.reconnect_delay_set(RECONNECT_MIN_DELAY, RECONNECT_MAX_DELAY)
        client.on_connect = self._paho_on_connect
        client.on_disconnect = self._paho_on_disconnect
        client.on_message = self._paho_on_message
        client.on_subscribe = self._paho_on_subscribe
        client.connect_async(self._host, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
        self._client = client
        client.loop_start()

    # Callbacks below run in the paho network thread.

    def _paho_on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: mqtt.ConnectFlags,
        reason_code: ReasonCode,
        properties: Any,
    ) -> None:
        if reason_code.is_failure:
            self._call_in_loop(
                self._handle_connect_failure, reason_code.value, str(reason_code)
            )
            return
        client.subscribe(
            [
                (TOPIC_RPC_REPLY.format(uid=self._uid), 2),
                (TOPIC_APP_DOWN.format(uid=self._uid), 2),
            ]
        )
        self._call_in_loop(self._handle_connected)

    def _paho_on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: mqtt.DisconnectFlags,
        reason_code: ReasonCode,
        properties: Any,
    ) -> None:
        self._call_in_loop(self._handle_disconnected, str(reason_code))

    def _paho_on_subscribe(
        self,
        client: mqtt.Client,
        userdata: Any,
        mid: int,
        reason_codes: list[ReasonCode],
        properties: Any,
    ) -> None:
        if any(reason_code.is_failure for reason_code in reason_codes):
            self._call_in_loop(
                _LOGGER.warning,
                "Kaadas cloud refused the event subscription: %s",
                [str(reason_code) for reason_code in reason_codes],
            )

    def _paho_on_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        message: mqtt.MQTTMessage,
    ) -> None:
        self._call_in_loop(self._on_message, message.topic, message.payload)

    def _call_in_loop(self, func: Callable[..., Any], *args: Any) -> None:
        # The event loop is already closed while Home Assistant shuts down.
        with contextlib.suppress(RuntimeError):
            self._hass.loop.call_soon_threadsafe(func, *args)

    # Callbacks below run in the event loop.

    @callback
    def _handle_connected(self) -> None:
        if self._client is None:
            # A connection that completed after async_disconnect was called.
            return
        _LOGGER.debug("Connected to %s", self._host)
        self._auth_refusals = 0
        if self._first_connect is not None and not self._first_connect.done():
            self._first_connect.set_result(None)
        self._set_connected(True)

    @callback
    def _handle_disconnected(self, reason: str) -> None:
        if self._client is not None:
            _LOGGER.debug("Disconnected from %s: %s", self._host, reason)
        self._set_connected(False)

    @callback
    def _handle_connect_failure(self, reason_value: int, reason: str) -> None:
        if self._client is None:
            return
        if reason_value not in AUTH_FAILURE_REASONS:
            # paho keeps retrying; transient failures resolve themselves.
            _LOGGER.debug("Connection to %s refused: %s", self._host, reason)
            return

        self._auth_refusals += 1
        _LOGGER.debug(
            "Connection to %s refused (%s of %s): %s",
            self._host,
            self._auth_refusals,
            AUTH_FAILURE_LIMIT,
            reason,
        )
        if self._auth_refusals != AUTH_FAILURE_LIMIT:
            return

        if self._first_connect is not None:
            if not self._first_connect.done():
                self._first_connect.set_exception(KaadasAuthError(reason))
            return

        _LOGGER.warning("Kaadas cloud rejected the session token: %s", reason)
        self._on_auth_failure()

    @callback
    def _set_connected(self, connected: bool) -> None:
        if connected == self._connected:
            return
        self._connected = connected
        self._on_connection_change(connected)


def _stop_client(client: mqtt.Client) -> None:
    client.disconnect()
    client.loop_stop()
