"""Butoane pentru valorile instantanee ale contorului inteligent."""

from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from . import ReteleElectriceConfigEntry
from .api import ReteleElectriceError
from .entity import ReteleElectriceEntity

_LOGGER = logging.getLogger(__name__)

# După o solicitare, recitim valorile la aceste intervale (secunde): portalul
# spune „în cel mult 2 ore”, dar de obicei datele vin mult mai repede.
_REREAD_AFTER = (5 * 60, 30 * 60, 2 * 60 * 60 + 5 * 60)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ReteleElectriceConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[ButtonEntity] = []
    for pod, data in (coordinator.data or {}).items():
        if data.get("smart"):
            entities.append(ReteleElectriceRequestButton(coordinator, pod))
            entities.append(ReteleElectriceRefreshButton(coordinator, pod))
    async_add_entities(entities)


async def async_refresh_instant(coordinator, pod: str) -> list | None:
    """Recitește valorile instantanee pentru un POD și actualizează entitățile."""
    pod_data = (coordinator.data or {}).get(pod) or {}
    code = str((pod_data.get("info") or {}).get("Fiscal_Code__c") or "")
    values = await coordinator.client.async_get_meter_instant(pod, code)
    data = dict(coordinator.data or {})
    updated = dict(data.get(pod) or {})
    updated["instant"] = values
    data[pod] = updated
    coordinator.async_set_updated_data(data)
    return values


class ReteleElectriceRefreshButton(ReteleElectriceEntity, ButtonEntity):
    """Recitește valorile instantanee deja disponibile pe portal."""

    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator, pod: str) -> None:
        super().__init__(coordinator, pod, "actualizare_instantanee")

    async def async_press(self) -> None:
        try:
            values = await async_refresh_instant(self.coordinator, self.pod)
        except ReteleElectriceError as err:
            raise HomeAssistantError(f"Nu am putut citi valorile instantanee: {err}") from err
        if not values:
            _LOGGER.info(
                "%s: portalul nu are încă valori instantanee; folosește butonul "
                "„Solicită citire contor” și așteaptă procesarea",
                self.pod,
            )


class ReteleElectriceRequestButton(ReteleElectriceEntity, ButtonEntity):
    """Cere contorului o citire nouă (max. 10 solicitări / 24 h pe portal)."""

    _attr_icon = "mdi:meter-electric-outline"

    def __init__(self, coordinator, pod: str) -> None:
        super().__init__(coordinator, pod, "solicita_citire")

    async def async_press(self) -> None:
        code = str((self.pod_data.get("info") or {}).get("Fiscal_Code__c") or "")
        try:
            row = await self.coordinator.client.async_request_meter_instant(self.pod, code)
        except ReteleElectriceError as err:
            raise HomeAssistantError(str(err)) from err

        now: datetime = dt_util.now()
        self.coordinator.instant_requests[self.pod] = {
            "ultima_solicitare": now.isoformat(timespec="seconds"),
            "estimare_portal": row.get("ESTIMATION"),
        }
        _LOGGER.info(
            "%s: solicitare trimisă (estimare portal: %s); recitesc automat valorile",
            self.pod,
            row.get("ESTIMATION"),
        )
        self.coordinator.async_update_listeners()

        for delay in _REREAD_AFTER:
            self.async_on_remove(
                async_call_later(self.hass, delay, self._make_reread())
            )

    def _make_reread(self):
        @callback
        def _reread(_now) -> None:
            self.hass.async_create_task(self._async_reread())

        return _reread

    async def _async_reread(self) -> None:
        try:
            await async_refresh_instant(self.coordinator, self.pod)
        except ReteleElectriceError as err:
            _LOGGER.debug("%s: recitirea valorilor instantanee a eșuat: %s", self.pod, err)
