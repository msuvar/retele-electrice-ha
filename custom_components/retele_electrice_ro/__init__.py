"""Integrare Home Assistant pentru portalul Rețele Electrice România."""

from __future__ import annotations

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import ReteleElectriceClient
from .const import DOMAIN, PLATFORMS
from .coordinator import ReteleElectriceCoordinator

type ReteleElectriceConfigEntry = ConfigEntry[ReteleElectriceCoordinator]


def create_client(hass: HomeAssistant, username: str, password: str) -> ReteleElectriceClient:
    """Client cu propriul cookie jar (sesiunea Salesforce nu se amestecă cu altele)."""
    session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
    return ReteleElectriceClient(session, username, password)


async def async_setup_entry(hass: HomeAssistant, entry: ReteleElectriceConfigEntry) -> bool:
    client = create_client(hass, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    coordinator = ReteleElectriceCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: ReteleElectriceConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ReteleElectriceConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
