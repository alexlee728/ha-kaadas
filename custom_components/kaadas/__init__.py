"""Kaadas Smart integration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, PLATFORMS
from .exceptions import KaadasAuthError, KaadasConnectionError
from .hub import KaadasConfigEntry, KaadasHub


async def async_setup_entry(hass: HomeAssistant, entry: KaadasConfigEntry) -> bool:
    """Set up Kaadas Smart from a config entry."""
    hub = KaadasHub(hass, entry)
    try:
        await hub.async_start()
    except KaadasAuthError as err:
        # The broker may refuse a valid token while its auth backend is down,
        # so ask the user to sign in again but keep retrying meanwhile.
        entry.async_start_reauth(hass)
        raise ConfigEntryNotReady(str(err)) from err
    except KaadasConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = hub
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        # Home Assistant does not run unload callbacks for a failed setup.
        await hub.async_stop()
        raise

    hub.async_start_event_delivery()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: KaadasConfigEntry) -> bool:
    """Unload a config entry."""
    hub = entry.runtime_data
    # No new lock may add entities to a platform being unloaded.
    hub.discovery_enabled = False
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hub.discovery_enabled = True
        return False
    await hub.async_stop()
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: KaadasConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow removing a lock; it is added again if it reports later."""
    # The entry may not be loaded, for example while setup is being retried.
    hub: KaadasHub | None = getattr(entry, "runtime_data", None)
    if hub is not None:
        for domain, device_id in device.identifiers:
            if domain == DOMAIN:
                hub.async_remove_device(device_id)
    return True
