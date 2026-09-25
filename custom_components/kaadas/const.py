"""Constants for the Kaadas Smart integration."""

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "kaadas"
MANUFACTURER = "Kaadas"

PLATFORMS = [Platform.BINARY_SENSOR, Platform.EVENT, Platform.SENSOR]

CONF_ACCOUNT = "account"
CONF_REGION = "region"
CONF_UID = "uid"
CONF_TOKEN = "token"


class Region(StrEnum):
    """Kaadas cloud region."""

    CN = "cn"
    SG = "sg"


@dataclass(frozen=True, slots=True)
class RegionEndpoints:
    """Cloud endpoints serving one region."""

    http_base: str
    mqtt_host: str
    language: str


ENDPOINTS: dict[Region, RegionEndpoints] = {
    Region.CN: RegionEndpoints(
        http_base="https://app.kaadas.com:34000",
        mqtt_host="appmq.kaadas.com",
        language="zh_CN",
    ),
    Region.SG: RegionEndpoints(
        http_base="https://app-sg.kaadas.com",
        mqtt_host="appmq-sg.kaadas.com",
        language="en_US",
    ),
}

MQTT_PORT = 5883
MQTT_KEEPALIVE = 60
MQTT_CONNECT_TIMEOUT = 15

APP_VERSION = "6.9.6"
BASE_KEY = "3a79fee83a79fbc3"

# The broker replays messages queued while Home Assistant was offline right
# after each connect. Messages received within REPLAY_WINDOW of a connect and
# sent more than MAX_EVENT_AGE earlier update state but fire no events.
REPLAY_WINDOW = timedelta(seconds=10)
MAX_EVENT_AGE = timedelta(seconds=60)

# Number of raw messages kept for the diagnostics download.
RECENT_MESSAGE_LIMIT = 50
