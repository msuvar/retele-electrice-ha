"""Entitate de bază: un dispozitiv per POD."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import ReteleElectriceCoordinator


class ReteleElectriceEntity(CoordinatorEntity[ReteleElectriceCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: ReteleElectriceCoordinator, pod: str, key: str) -> None:
        super().__init__(coordinator)
        self.pod = pod
        self._attr_unique_id = f"{pod}_{key}"
        self._attr_translation_key = key
        info = self.pod_data.get("info", {}) if self.pod_data else {}
        city = info.get("City__c")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, pod)},
            name=f"POD {pod}" + (f" ({city.title()})" if city else ""),
            manufacturer=MANUFACTURER,
            model=info.get("EA_METER_TYPE__c") or "Loc de consum",
            serial_number=info.get("EA_METER_SERIE__c"),
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://contulmeu.reteleelectrice.ro/s/",
        )

    @property
    def pod_data(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self.pod, {})

    @property
    def available(self) -> bool:
        return super().available and bool(self.pod_data)
