"""Diagnostics for Kaadas Smart.

The download includes recent raw push messages so that event codes of lock
models not yet supported can be identified. Users attach it to public issues,
so account, device and person identifiers are removed.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_ACCOUNT, CONF_TOKEN, CONF_UID
from .hub import KaadasConfigEntry

TO_REDACT = {
    # Config entry
    CONF_ACCOUNT,
    CONF_TOKEN,
    CONF_UID,
    # Device state
    "device_id",
    "serial_number",
    "name",
    # Push messages
    "wfId",
    "lockId",
    "sn",
    "lockNickname",
    "userNickName",
    "pwdNickName",
    "wifiSSID",
    "BISSID",
    "mac",
}

# The account UID also appears in topics and possibly other fields. Shorter
# secrets would match unrelated text such as timestamps.
MIN_SECRET_LENGTH = 6


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: KaadasConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    hub = entry.runtime_data
    data = {
        "entry": dict(entry.data),
        "connected": hub.connected,
        "devices": [asdict(state) for state in hub.devices.values()],
        "recent_messages": [
            {
                "received_at": message.received_at.isoformat(),
                "topic": message.topic,
                "payload": message.payload,
            }
            for message in hub.recent_messages
        ],
    }
    return _replace_secret(
        async_redact_data(data, TO_REDACT), entry.data[CONF_UID], REDACTED
    )


def _replace_secret(data: Any, secret: str, replacement: str) -> Any:
    """Replace a secret wherever it appears, including inside strings."""
    if isinstance(data, str | int) and not isinstance(data, bool):
        if str(data) == secret:
            return replacement
        if isinstance(data, str) and len(secret) >= MIN_SECRET_LENGTH:
            return data.replace(secret, replacement)
        return data
    if isinstance(data, dict):
        return {
            key: _replace_secret(value, secret, replacement)
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [_replace_secret(value, secret, replacement) for value in data]
    return data
