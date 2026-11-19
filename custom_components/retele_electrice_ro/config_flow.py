"""Config flow pentru Rețele Electrice România."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .api import ReteleElectriceAuthError, ReteleElectriceError
from .const import (
    CONF_PODS,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


def _pod_label(pod: dict[str, Any]) -> str:
    name = pod.get("POD__c") or pod.get("Name")
    city = pod.get("City__c") or ""
    smart = " · smart meter" if pod.get("Smart_meter__c") else ""
    return f"{name} — {city}{smart}".strip(" —")


async def _validate(hass, username: str, password: str) -> dict[str, str]:
    """Verifică credențialele și întoarce {pod: eticheta}."""
    from . import create_client  # import local ca să evităm cicluri

    client = create_client(hass, username, password)
    await client.login()
    pods = await client.async_get_pods()
    return {(p.get("POD__c") or p.get("Name")): _pod_label(p) for p in pods}


class ReteleElectriceConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._user: dict[str, Any] = {}
        self._pods: dict[str, str] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            await self.async_set_unique_id(username.lower())
            self._abort_if_unique_id_configured()
            try:
                self._pods = await _validate(self.hass, username, user_input[CONF_PASSWORD])
            except ReteleElectriceAuthError as err:
                _LOGGER.warning("Autentificare eșuată pe portal: %s", err)
                errors["base"] = "invalid_auth"
            except ReteleElectriceError:
                _LOGGER.exception("Eroare la conectarea la portal")
                errors["base"] = "cannot_connect"
            else:
                if not self._pods:
                    errors["base"] = "no_pods"
                else:
                    self._user = {**user_input, CONF_USERNAME: username}
                    return await self.async_step_pods()

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_pods(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=self._user[CONF_USERNAME],
                data={**self._user, CONF_PODS: user_input[CONF_PODS]},
            )
        schema = vol.Schema(
            {vol.Required(CONF_PODS, default=list(self._pods)): cv.multi_select(self._pods)}
        )
        return self.async_show_form(step_id="pods", data_schema=schema)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await _validate(self.hass, entry.data[CONF_USERNAME], user_input[CONF_PASSWORD])
            except ReteleElectriceAuthError:
                errors["base"] = "invalid_auth"
            except ReteleElectriceError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ReteleElectriceOptionsFlow()


class ReteleElectriceOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["settings", "credentials"])

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        if user_input is not None:
            return self.async_create_entry(data={**entry.options, **user_input})

        errors: dict[str, str] = {}
        try:
            pods = await _validate(
                self.hass, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
            )
        except ReteleElectriceError:
            pods = {p: p for p in entry.data.get(CONF_PODS, [])}
            errors["base"] = "cannot_connect"

        current_pods = entry.options.get(CONF_PODS, entry.data.get(CONF_PODS, list(pods)))
        current_interval = entry.options.get(
            CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=current_interval): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
                ),
                vol.Required(
                    CONF_PODS, default=[p for p in current_pods if p in pods]
                ): cv.multi_select(pods),
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema, errors=errors)

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Schimbă emailul și/sau parola fără reinstalare."""
        entry = self.config_entry
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            password = user_input.get(CONF_PASSWORD) or entry.data[CONF_PASSWORD]
            new_uid = username.lower()
            clash = any(
                e.unique_id == new_uid and e.entry_id != entry.entry_id
                for e in self.hass.config_entries.async_entries(DOMAIN)
            )
            if clash:
                errors["base"] = "already_configured"
            else:
                try:
                    await _validate(self.hass, username, password)
                except ReteleElectriceAuthError as err:
                    _LOGGER.warning("Autentificare eșuată pe portal: %s", err)
                    errors["base"] = "invalid_auth"
                except ReteleElectriceError:
                    errors["base"] = "cannot_connect"
                else:
                    self.hass.config_entries.async_update_entry(
                        entry,
                        title=username,
                        unique_id=new_uid,
                        data={**entry.data, CONF_USERNAME: username, CONF_PASSWORD: password},
                    )
                    # Salvarea opțiunilor declanșează reîncărcarea integrării.
                    return self.async_create_entry(data=dict(entry.options))

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=entry.data[CONF_USERNAME]): str,
                vol.Optional(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="credentials", data_schema=schema, errors=errors
        )
