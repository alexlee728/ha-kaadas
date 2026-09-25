"""Tests for the Kaadas Smart config flow."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kaadas.api import LoginResult
from custom_components.kaadas.const import (
    CONF_ACCOUNT,
    CONF_REGION,
    CONF_TOKEN,
    CONF_UID,
    DOMAIN,
)
from custom_components.kaadas.exceptions import KaadasAuthError, KaadasConnectionError

from .conftest import ACCOUNT, UID

USER_INPUT = {CONF_ACCOUNT: f" {ACCOUNT} ", CONF_PASSWORD: "secret", CONF_REGION: "sg"}


@pytest.fixture
def mock_login() -> Generator[AsyncMock]:
    """Patch the cloud login."""
    with patch(
        "custom_components.kaadas.config_flow.async_login",
        return_value=LoginResult(uid=UID, token="new-token"),
    ) as login:
        yield login


@pytest.fixture(autouse=True)
def mock_setup_entry() -> Generator[AsyncMock]:
    """Keep created entries from connecting."""
    with patch(
        "custom_components.kaadas.async_setup_entry", return_value=True
    ) as setup_entry:
        yield setup_entry


async def test_user_flow(hass: HomeAssistant, mock_login: AsyncMock) -> None:
    """A successful login creates an entry keyed by the account UID."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == ACCOUNT
    assert result["result"].unique_id == UID
    assert result["data"] == {
        CONF_ACCOUNT: ACCOUNT,
        CONF_REGION: "sg",
        CONF_UID: UID,
        CONF_TOKEN: "new-token",
    }
    assert mock_login.call_args.args[1:] == (ACCOUNT, "secret", "sg")


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (KaadasAuthError, "invalid_auth"),
        (KaadasConnectionError, "cannot_connect"),
        (RuntimeError, "unknown"),
    ],
)
async def test_user_flow_errors_recover(
    hass: HomeAssistant,
    mock_login: AsyncMock,
    error: type[Exception],
    reason: str,
) -> None:
    """Login errors are shown on the form and the user can retry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    mock_login.side_effect = error
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": reason}

    mock_login.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_already_configured(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_login: AsyncMock
) -> None:
    """The same account cannot be added twice."""
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_token(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_login: AsyncMock
) -> None:
    """Reauthentication stores the new token for the same account."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "secret"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_TOKEN] == "new-token"
    assert mock_login.call_args.args[1:] == (ACCOUNT, "secret", "cn")


async def test_reauth_rejects_other_account(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_login: AsyncMock
) -> None:
    """Credentials of another account do not replace the token."""
    config_entry.add_to_hass(hass)
    mock_login.return_value = LoginResult(uid="someone-else", token="other")
    result = await config_entry.start_reauth_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "secret"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert config_entry.data[CONF_TOKEN] != "other"
