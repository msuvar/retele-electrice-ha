"""Senzor binar: întrerupere în alimentare raportată pentru POD."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ReteleElectriceConfigEntry
from .entity import ReteleElectriceEntity

# Mesajul afișat de portal când nu există nicio întrerupere înregistrată.
_NO_OUTAGE_MARKERS = ("nicio întrerupere", "nicio intrerupere", "no interruption")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ReteleElectriceConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        ReteleElectriceOutage(coordinator, pod) for pod in (coordinator.data or {})
    )


class ReteleElectriceOutage(ReteleElectriceEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator, pod: str) -> None:
        super().__init__(coordinator, pod, "intrerupere_activa")

    @property
    def is_on(self) -> bool | None:
        outage = self.pod_data.get("outage")
        if not outage:
            return None
        msg = (outage.get("message") or "").lower()
        if not msg:
            return None
        return not any(m in msg for m in _NO_OUTAGE_MARKERS)

    @property
    def extra_state_attributes(self):
        outage = self.pod_data.get("outage") or {}
        return {"mesaj": outage.get("message")}
