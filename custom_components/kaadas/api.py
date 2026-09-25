"""Kaadas cloud HTTP API."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import time
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import APP_VERSION, BASE_KEY, ENDPOINTS, Region
from .exceptions import KaadasAuthError, KaadasConnectionError

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
AUTH_FAILURE_STATUSES = frozenset({401, 403, 444})


@dataclass(frozen=True, slots=True)
class LoginResult:
    """Credentials returned by a successful login."""

    uid: str
    token: str


async def async_login(
    session: aiohttp.ClientSession,
    account: str,
    password: str,
    region: Region,
) -> LoginResult:
    """Log in with an email address or phone number."""
    endpoints = ENDPOINTS[region]
    timestamp = str(int(time.time()))

    if "@" in account:
        path = "/user/login/getuserbymail"
        body = {"mail": account, "password": password}
    else:
        path = "/user/login/getuserbytel"
        body = {"tel": account, "password": password}

    headers = {
        "Content-Type": "application/json",
        "reqSource": "app",
        "timestamp": timestamp,
        "version": "1",
        "ignore": "token",
        "lang": endpoints.language,
        "clientAPPVersion": APP_VERSION,
    }

    try:
        async with session.post(
            endpoints.http_base + path,
            data=_encrypt(body, timestamp),
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        ) as response:
            if response.status in AUTH_FAILURE_STATUSES:
                raise KaadasAuthError(f"Login rejected with HTTP {response.status}")
            response.raise_for_status()
            data = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise KaadasConnectionError(f"Login request failed: {err}") from err

    result = data.get("data") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        result = {}
    uid = str(result.get("uid") or "")
    token = str(result.get("token") or "")
    if not uid or not token:
        raise KaadasAuthError("Login response did not contain credentials")

    return LoginResult(uid=uid, token=token)


def _request_key(timestamp: str) -> bytes:
    """Derive the per-request AES key from the request timestamp."""
    return (BASE_KEY[:5] + timestamp[-6:-3] + BASE_KEY[5:10] + timestamp[-3:]).encode()


def _encrypt(body: dict[str, Any], timestamp: str) -> bytes:
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(raw) + padder.finalize()
    encryptor = Cipher(algorithms.AES(_request_key(timestamp)), modes.ECB()).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize())
