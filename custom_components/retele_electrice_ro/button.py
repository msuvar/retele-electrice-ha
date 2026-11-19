"""Buton: reîmprospătează valorile instantanee ale contorului inteligent."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ReteleElectriceConfigEntry
from .api import ReteleElectriceError
from .entity import ReteleElectriceEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ReteleElectriceConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        ReteleElectriceInstantButton(coordinator, pod)
        for pod, data in (coordinator.data or {}).items()
        if data.get("smart")
    )


class ReteleElectriceInstantButton(ReteleElectriceEntity, ButtonEntity):
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator, pod: str) -> None:
        super().__init__(coordinator, pod, "actualizare_instantanee")

    async def async_press(self) -> None:
        info = self.pod_data.get("info") or {}
        code = str(info.get("Fiscal_Code__c") or "")
        try:
            values = await self.coordinator.client.async_get_meter_instant(self.pod, code)
        except ReteleElectriceError as err:
            raise HomeAssistantError(f"Nu am putut citi valorile instantanee: {err}") from err

        data = dict(self.coordinator.data or {})
        pod_data = dict(data.get(self.pod) or {})
        pod_data["instant"] = values
        data[self.pod] = pod_data
        self.coordinator.async_set_updated_data(data)
        if not values:
            _LOGGER.info(
                "%s: portalul nu are valori instantanee; solicită-le din portal "
                "(„Solicită datele de consum”) și încearcă din nou peste câteva ore",
                self.pod,
            )
