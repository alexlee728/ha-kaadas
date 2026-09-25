# Kaadas Smart for Home Assistant

Unofficial Home Assistant integration for Kaadas smart locks. It receives
real-time push events from the Kaadas cloud and is read-only: it cannot unlock
the door.

Tested with **Kaadas P30 Pro Max**. Other Kaadas cloud-connected locks may work
but are not yet verified.

## Entities

Each lock is added as a device with these entities:

| Entity | Type | Description |
|---|---|---|
| Lock | Binary sensor | On when the lock is unlocked. The last known state is kept across restarts. |
| Unlock | Event | Fires on every unlock. The event type is the unlock method (fingerprint, face, visitor, mechanical key, open button, other); the attributes name the user and credential. |
| Doorbell | Event | Fires when the doorbell button on the lock is pressed. |
| Alarm | Event | Fires when the lock raises any other alarm; the `alarm_code` attribute holds the raw code. |
| Device problem | Binary sensor | Diagnostic. On when the lock reports an error code. |
| Wi-Fi signal | Sensor | Diagnostic, disabled by default. Updated only when the lock wakes up. |

While Home Assistant is offline the Kaadas cloud queues the lock's messages
and delivers them on the next connection. Those queued messages update the
lock state, but a queued message sent more than 60 seconds earlier does not
fire an event, so a doorbell press from an hour ago does not trigger
automations. Recognising a queued message relies on the time the lock reports:
if the lock clock is off by more than a minute, a queued event may still fire,
or a real event in the first seconds after a reconnect may be skipped. Messages
received later always fire.

If the Kaadas cloud repeatedly rejects the session, Home Assistant asks you to
sign in again but keeps retrying in the background. If the rejection was
temporary, the request disappears once the connection is back.

A lock appears after its first push message. Status reports do not include the
lock's nickname, so a lock may first be named `Kaadas <ID>` and be renamed to
its nickname after the next unlock; its entity IDs keep the first name.

A lock that is no longer used can be deleted from its device page. It is added
again if it reports later.

### Automation example

An event entity also changes state when it becomes available again, for
example after a restart or a reconnect. Exclude those transitions with
`not_from`:

```yaml
triggers:
  - trigger: state
    entity_id: event.front_door_unlock
    not_from:
      - unavailable
      - unknown
conditions:
  - condition: state
    entity_id: event.front_door_unlock
    attribute: event_type
    state: fingerprint
actions:
  - action: notify.mobile_app_phone
    data:
      message: "{{ state_attr('event.front_door_unlock', 'user') }} is home"
```

## Requirements

- Home Assistant 2025.3 or later.
- A Kaadas Smart account dedicated to Home Assistant. Share the lock with it
  from the Kaadas app. When Home Assistant and the Kaadas app use the same
  account, they may disconnect each other.

## Installation

### HACS

1. Add `https://github.com/alexlee728/ha-kaadas` as a custom **Integration** repository.
2. Install **Kaadas Smart**.
3. Restart Home Assistant.
4. Go to **Settings → Devices & services → Add Integration** and search for **Kaadas Smart**.

### Manual

Copy `custom_components/kaadas` to your Home Assistant `custom_components`
directory, then restart Home Assistant.

## Reporting an unsupported lock

Go to **Settings → Devices & services → Kaadas Smart**, open the menu of the
account entry, select **Download diagnostics**, and attach the file to an
issue. It contains the recent raw push messages, which is what is needed to map
the event codes of other models. Account credentials, device identifiers, Wi-Fi
network details, MAC addresses, lock names, and user and credential names are
removed; review the file before posting it.

## Development

```bash
pip install -r requirements_test.txt
ruff check . && ruff format --check .
pytest
```

## Disclaimer

This is an unofficial community integration and is not affiliated with or
endorsed by Kaadas.

## License

MIT
