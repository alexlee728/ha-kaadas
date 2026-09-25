"""Tests for setup, the state hub and the entities it feeds."""

from __future__ import annotations

from datetime import timedelta
import json
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    restore_state,
)
from homeassistant.helpers.json import ExtendedJSONEncoder
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.kaadas import async_remove_config_entry_device
from custom_components.kaadas.const import DOMAIN, REPLAY_WINDOW
from custom_components.kaadas.diagnostics import async_get_config_entry_diagnostics
from custom_components.kaadas.exceptions import KaadasAuthError, KaadasConnectionError

from .conftest import (
    LOCK_BODY_ID,
    LOCK_MAC,
    LOCK_NAME,
    TOKEN,
    UID,
    WIFI_BSSID,
    WIFI_ID,
    WIFI_SSID,
    FakeClientFactory,
    FakeMqttClient,
    alarm,
    lock_event,
    lock_info,
    record,
)

# Status reports carry no nickname, so a lock found through one is named after
# its Wi-Fi module ID and its entity IDs keep that name after a rename.
DEFAULT_NAME = f"Kaadas {WIFI_ID}"
LOCK = "binary_sensor.kaadas_wf0000000001_lock"
PROBLEM = "binary_sensor.kaadas_wf0000000001_device_problem"
UNLOCK = "event.kaadas_wf0000000001_unlock"
DOORBELL = "event.kaadas_wf0000000001_doorbell"
ALARM = "event.kaadas_wf0000000001_alarm"
RSSI = "sensor.kaadas_wf0000000001_wi_fi_signal"


async def test_setup_connects_with_entry_credentials(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    setup_integration: FakeMqttClient,
) -> None:
    """The client uses the regional broker and the stored token."""
    assert config_entry.state is ConfigEntryState.LOADED
    assert setup_integration.host == "appmq.kaadas.com"
    assert setup_integration.uid == UID
    assert setup_integration.token == TOKEN


async def test_first_message_creates_device_and_entities(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    config_entry: MockConfigEntry,
    setup_integration: FakeMqttClient,
) -> None:
    """A lock is discovered from its first push message."""
    setup_integration.push(lock_info())
    await hass.async_block_till_done()

    device = _lock_device(device_registry, config_entry)
    assert device is not None
    assert device.name == DEFAULT_NAME
    assert device.model == "K3AWE"
    assert device.serial_number == WIFI_ID
    assert device.manufacturer == "Kaadas"

    assert hass.states.get(LOCK).state == STATE_UNKNOWN
    assert hass.states.get(PROBLEM).state == STATE_OFF
    for entity_id in (UNLOCK, DOORBELL, ALARM):
        assert hass.states.get(entity_id).state == STATE_UNKNOWN

    rssi = entity_registry.async_get(RSSI)
    assert rssi.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_nickname_renames_device(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    config_entry: MockConfigEntry,
    setup_integration: FakeMqttClient,
) -> None:
    """A record with the lock nickname renames the device; IDs stay stable."""
    setup_integration.push(lock_info())
    setup_integration.push(record(2, 4, lockNickname=LOCK_NAME))
    await hass.async_block_till_done()

    device = _lock_device(device_registry, config_entry)
    assert device.name == LOCK_NAME
    # Records carry a different lockId than status reports; the serial number
    # comes from the status report only.
    assert device.serial_number == WIFI_ID
    assert hass.states.get(LOCK).state == STATE_ON


async def test_unlock_and_lock(
    hass: HomeAssistant, setup_integration: FakeMqttClient
) -> None:
    """Unlocking fires the unlock event and locking updates the lock sensor."""
    setup_integration.push(lock_info())
    await hass.async_block_till_done()

    setup_integration.push(record(2, 4, userNickName="Alex", pwdNickName="Right thumb"))
    await hass.async_block_till_done()

    assert hass.states.get(LOCK).state == STATE_ON
    unlock = hass.states.get(UNLOCK)
    assert unlock.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
    assert unlock.attributes["event_type"] == "fingerprint"
    assert unlock.attributes["user"] == "Alex"
    assert unlock.attributes["credential"] == "Right thumb"

    setup_integration.push(record(1, 0))
    await hass.async_block_till_done()

    assert hass.states.get(LOCK).state == STATE_OFF


async def test_doorbell_is_not_an_alarm(
    hass: HomeAssistant, setup_integration: FakeMqttClient
) -> None:
    """Doorbell presses and other alarms fire separate entities."""
    setup_integration.push(lock_info())
    await hass.async_block_till_done()

    setup_integration.push(alarm(96))
    await hass.async_block_till_done()
    assert hass.states.get(DOORBELL).attributes["event_type"] == "ring"
    assert hass.states.get(ALARM).state == STATE_UNKNOWN

    setup_integration.push(alarm(3))
    await hass.async_block_till_done()
    assert hass.states.get(ALARM).attributes["alarm_code"] == 3


async def test_replayed_messages_update_state_without_firing_events(
    hass: HomeAssistant, setup_integration: FakeMqttClient
) -> None:
    """Messages queued while offline must not trigger doorbell automations."""
    setup_integration.push(lock_info())
    await hass.async_block_till_done()

    an_hour_ago = dt_util.utcnow() - timedelta(hours=1)
    setup_integration.push(alarm(96, timestamp=an_hour_ago))
    setup_integration.push(record(2, 4, timestamp=an_hour_ago))
    await hass.async_block_till_done()

    assert hass.states.get(DOORBELL).state == STATE_UNKNOWN
    assert hass.states.get(UNLOCK).state == STATE_UNKNOWN
    assert hass.states.get(LOCK).state == STATE_ON


async def test_out_of_order_record_is_ignored(
    hass: HomeAssistant, setup_integration: FakeMqttClient
) -> None:
    """A record older than the applied state does not overwrite it."""
    now = dt_util.utcnow()
    setup_integration.push(record(1, 0, timestamp=now))
    setup_integration.push(record(2, 4, timestamp=now - timedelta(minutes=5)))
    await hass.async_block_till_done()

    assert hass.states.get(LOCK).state == STATE_OFF


async def test_message_with_lock_id_only_uses_learned_device(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    config_entry: MockConfigEntry,
    setup_integration: FakeMqttClient,
) -> None:
    """A lock ID seen with a device ID resolves later messages to that device."""
    setup_integration.push(lock_info())
    payload = record(2, 4, lockId=LOCK_BODY_ID)
    del payload["wfId"]
    setup_integration.push(payload)
    await hass.async_block_till_done()

    assert (
        len(dr.async_entries_for_config_entry(device_registry, config_entry.entry_id))
        == 1
    )
    assert hass.states.get(LOCK).state == STATE_ON


async def test_malformed_messages_are_ignored(
    hass: HomeAssistant, setup_integration: FakeMqttClient
) -> None:
    """Garbage on the topic neither raises nor changes state."""
    setup_integration.push(b"\xff not json")
    setup_integration.push([1, 2, 3])
    await hass.async_block_till_done()
    assert hass.states.get(LOCK) is None

    setup_integration.push(lock_event("record", "not a dict"))
    await hass.async_block_till_done()
    assert hass.states.get(LOCK).state == STATE_UNKNOWN


async def test_connection_loss_makes_entities_unavailable(
    hass: HomeAssistant, setup_integration: FakeMqttClient
) -> None:
    """Entities follow the cloud connection."""
    setup_integration.push(lock_info())
    await hass.async_block_till_done()

    setup_integration.set_connected(False)
    await hass.async_block_till_done()
    assert hass.states.get(LOCK).state == STATE_UNAVAILABLE

    setup_integration.set_connected(True)
    await hass.async_block_till_done()
    assert hass.states.get(LOCK).state == STATE_UNKNOWN


async def test_known_devices_and_state_survive_reload(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    config_entry: MockConfigEntry,
    mqtt_client: FakeClientFactory,
    setup_integration: FakeMqttClient,
) -> None:
    """After a reload entities exist before any message and keep their state."""
    setup_integration.push(lock_info())
    setup_integration.push(record(2, 4))
    await hass.async_block_till_done()

    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert mqtt_client.client is not setup_integration
    assert hass.states.get(LOCK).state == STATE_ON
    assert hass.states.get(UNLOCK).attributes["event_type"] == "fingerprint"
    assert config_entry.runtime_data.devices[WIFI_ID].serial_number == WIFI_ID


async def test_lock_state_restored_after_restart(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    config_entry: MockConfigEntry,
    mqtt_client: FakeClientFactory,
) -> None:
    """The saved lock state survives even if the entity was unavailable."""
    config_entry.add_to_hass(hass)
    device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, WIFI_ID)},
        name=DEFAULT_NAME,
    )
    saved_at = dt_util.utcnow().replace(microsecond=0) - timedelta(minutes=5)
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(LOCK, STATE_UNAVAILABLE),
                {"is_locked": False, "changed_at": saved_at.isoformat()},
            ),
        ),
    )
    # The broker replays a record older than the saved state.
    mqtt_client.replay_on_connect = (
        record(1, 0, timestamp=saved_at - timedelta(minutes=1)),
    )

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(LOCK).state == STATE_ON


async def test_rejected_token_at_setup_starts_reauth(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mqtt_client: FakeClientFactory,
) -> None:
    """A rejected token asks the user to sign in again and setup is retried."""
    mqtt_client.connect_error = KaadasAuthError("Not authorized")
    config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert _reauth_flows(hass)


async def test_unreachable_cloud_retries_setup(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mqtt_client: FakeClientFactory,
) -> None:
    """An unreachable broker makes Home Assistant retry setup."""
    mqtt_client.connect_error = KaadasConnectionError("timeout")
    config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_rejected_token_at_runtime_starts_reauth(
    hass: HomeAssistant, setup_integration: FakeMqttClient
) -> None:
    """A token rejected while running asks the user to sign in again."""
    setup_integration.set_connected(False)
    setup_integration.on_auth_failure()
    await hass.async_block_till_done()
    assert _reauth_flows(hass)

    # The broker accepts the token again; the request is withdrawn.
    setup_integration.set_connected(True)
    await hass.async_block_till_done()
    assert not _reauth_flows(hass)


async def test_diagnostics_redact_personal_data(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    setup_integration: FakeMqttClient,
) -> None:
    """Diagnostics keep raw messages but no account, device or person IDs."""
    setup_integration.push(lock_info())
    setup_integration.push(
        record(2, 4, lockNickname=LOCK_NAME, userNickName="Alex", pwdNickName="Thumb")
    )
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)

    dumped = json.dumps(diagnostics, cls=ExtendedJSONEncoder)
    for secret in (
        UID,
        TOKEN,
        WIFI_ID,
        LOCK_BODY_ID,
        LOCK_NAME,
        WIFI_SSID,
        WIFI_BSSID,
        LOCK_MAC,
        "Alex",
        "Thumb",
    ):
        assert secret not in dumped
    assert diagnostics["devices"][0]["model"] == "K3AWE"
    info, unlock = diagnostics["recent_messages"]
    assert info["payload"]["eventparams"]["RSSI"] == "-60"
    assert unlock["payload"]["eventparams"]["eventCode"] == 2
    assert unlock["payload"]["eventparams"]["eventSource"] == 4


async def test_events_replayed_during_setup_are_delivered(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mqtt_client: FakeClientFactory,
) -> None:
    """Fresh events replayed before the entities exist are not lost."""
    mqtt_client.replay_on_connect = (lock_info(), alarm(96))
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(DOORBELL).attributes["event_type"] == "ring"


async def test_live_messages_ignore_the_lock_clock(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    setup_integration: FakeMqttClient,
) -> None:
    """After the replay window, a lock clock running behind changes nothing."""
    freezer.tick(REPLAY_WINDOW + timedelta(seconds=1))
    now = dt_util.utcnow()

    setup_integration.push(record(2, 4, timestamp=now - timedelta(hours=1)))
    await hass.async_block_till_done()
    assert hass.states.get(LOCK).state == STATE_ON
    assert hass.states.get(UNLOCK).attributes["event_type"] == "fingerprint"

    setup_integration.push(record(1, 0, timestamp=now - timedelta(hours=2)))
    await hass.async_block_till_done()
    assert hass.states.get(LOCK).state == STATE_OFF


async def test_lock_clock_ahead_does_not_freeze_state(
    hass: HomeAssistant, setup_integration: FakeMqttClient
) -> None:
    """A record stamped in the future does not block later records."""
    now = dt_util.utcnow()
    setup_integration.push(record(1, 0, timestamp=now + timedelta(hours=8)))
    setup_integration.push(record(2, 4, timestamp=now))
    await hass.async_block_till_done()

    assert hass.states.get(LOCK).state == STATE_ON


async def test_setup_failure_after_connecting_disconnects(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mqtt_client: FakeClientFactory,
) -> None:
    """A failing platform setup does not leave the MQTT client running."""
    config_entry.add_to_hass(hass)
    with patch.object(
        hass.config_entries,
        "async_forward_entry_setups",
        side_effect=RuntimeError("boom"),
    ):
        assert not await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert mqtt_client.client.disconnect_calls == 1


async def test_unload_disconnects(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    setup_integration: FakeMqttClient,
) -> None:
    """Unloading the entry closes the MQTT connection."""
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.NOT_LOADED
    assert setup_integration.disconnect_calls == 1
    assert not setup_integration.connected


async def test_removed_device_is_forgotten(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    config_entry: MockConfigEntry,
    setup_integration: FakeMqttClient,
) -> None:
    """Deleting a lock removes its entities cleanly; it returns if it reports."""
    setup_integration.push(lock_info())
    setup_integration.push(record(2, 4))
    await hass.async_block_till_done()
    device = _lock_device(device_registry, config_entry)

    # Delete the device the way the UI does.
    assert await async_setup_component(hass, "config", {})
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "config/device_registry/remove", "device_id": device.id}
    )
    response = await client.receive_json()
    if not response["success"]:
        # Releases before 2026 only offer the per-config-entry command.
        assert response["error"]["code"] == "unknown_command"
        await client.send_json_auto_id(
            {
                "type": "config/device_registry/remove_config_entry",
                "config_entry_id": config_entry.entry_id,
                "device_id": device.id,
            }
        )
        response = await client.receive_json()
    assert response["success"]
    await hass.async_block_till_done()

    assert hass.states.get(LOCK) is None
    assert entity_registry.async_get(LOCK) is None
    assert WIFI_ID not in config_entry.runtime_data.devices
    # Saving restore state for the whole instance still works.
    restore_state.async_get(hass).async_get_stored_states()

    setup_integration.push(lock_info())
    await hass.async_block_till_done()
    assert hass.states.get(LOCK) is not None


async def test_remove_device_while_not_loaded(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    config_entry: MockConfigEntry,
    mqtt_client: FakeClientFactory,
) -> None:
    """A lock can be deleted while setup is being retried."""
    mqtt_client.connect_error = KaadasConnectionError("timeout")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    device = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={(DOMAIN, WIFI_ID)}
    )

    assert await async_remove_config_entry_device(hass, config_entry, device)


async def test_slowly_drained_queue_is_still_a_replay(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    setup_integration: FakeMqttClient,
) -> None:
    """Each replayed message extends the replay window."""
    setup_integration.push(lock_info())
    old = dt_util.utcnow() - timedelta(hours=1)
    for _ in range(3):
        freezer.tick(REPLAY_WINDOW - timedelta(seconds=1))
        setup_integration.push(record(2, 4, timestamp=old))
        await hass.async_block_till_done()

    freezer.tick(REPLAY_WINDOW - timedelta(seconds=1))
    setup_integration.push(alarm(96, timestamp=old))
    await hass.async_block_till_done()

    assert hass.states.get(UNLOCK).state == STATE_UNKNOWN
    assert hass.states.get(DOORBELL).state == STATE_UNKNOWN


def _lock_device(
    device_registry: dr.DeviceRegistry, config_entry: MockConfigEntry
) -> dr.DeviceEntry | None:
    """Return the test lock's device of the config entry.

    Identifiers are only unique per config entry, and the lookup by identifier
    alone is deprecated in recent Home Assistant releases.
    """
    return next(
        (
            device
            for device in dr.async_entries_for_config_entry(
                device_registry, config_entry.entry_id
            )
            if (DOMAIN, WIFI_ID) in device.identifiers
        ),
        None,
    )


def _reauth_flows(hass: HomeAssistant) -> list:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["context"]["source"] == SOURCE_REAUTH
    ]
