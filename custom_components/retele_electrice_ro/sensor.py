"""Senzori pentru Rețele Electrice România."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ReteleElectriceConfigEntry
from .coordinator import to_float
from .entity import ReteleElectriceEntity

Data = dict[str, Any]


def _latest(d: Data) -> Data | None:
    r = d.get("readings") or []
    return r[0] if r else None


def _last_billed_consumption(d: Data) -> float | None:
    """Diferența dintre ultimele două citiri reale (de obicei pe 1 ale lunii)."""
    real = [r for r in d.get("readings") or [] if r.get("type") == "real"]
    if len(real) < 2:
        return None
    return round(real[0]["value"] - real[1]["value"], 3)


def _readings_attrs(d: Data) -> Data:
    latest = _latest(d)
    if not latest:
        return {}
    return {
        "data_citire": latest["date"].date().isoformat(),
        "tip_citire": latest["type"],
        "serie_contor": latest["serial"],
        "istoric": {
            r["date"].date().isoformat(): r["value"] for r in (d.get("readings") or [])[:24]
        },
    }


def _history_value(key: str, field: str = "SUM_EA") -> Callable[[Data], float | None]:
    def fn(d: Data) -> float | None:
        row = d.get(key)
        return to_float(row.get(field)) if row else None

    return fn


def _history_attrs(key: str) -> Callable[[Data], Data]:
    def fn(d: Data) -> Data:
        row = d.get(key) or {}
        return {
            "interval_start": row.get("START_DATE"),
            "interval_sfarsit": row.get("END_DATE"),
            "energie_produsa_kwh": to_float(row.get("SUM_EAP")),
            "energie_reactiva_inductiva_kvarh": to_float(row.get("SUM_ER")),
            "energie_reactiva_capacitiva_kvarh": to_float(row.get("SUM_ERC")),
            "maxim_ea": to_float(row.get("MAX_EA")),
        }

    return fn


def _instant_state(d: Data) -> str | None:
    values = d.get("instant")
    if not values:
        return None
    first = values[0] if isinstance(values, list) else values
    for k in ("readingDate", "READING_DATE", "data", "DATE", "timestamp"):
        if isinstance(first, dict) and first.get(k):
            return str(first[k])
    return f"{len(values)} valori"


def _outage_state(d: Data) -> str | None:
    o = d.get("outage")
    if not o:
        return None
    msg = o.get("message") or ""
    return msg[:250] if msg else o.get("check")


@dataclass(frozen=True, kw_only=True)
class REDescription(SensorEntityDescription):
    value_fn: Callable[[Data], Any]
    attrs_fn: Callable[[Data], Data] | None = None
    smart_only: bool = False


SENSORS: tuple[REDescription, ...] = (
    REDescription(
        key="index_energie",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=0,
        value_fn=lambda d: (_latest(d) or {}).get("value"),
        attrs_fn=_readings_attrs,
    ),
    REDescription(
        key="data_ultimei_citiri",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda d: (_latest(d) or {}).get("date") and _latest(d)["date"].date(),
    ),
    REDescription(
        key="consum_ultima_luna",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=0,
        value_fn=_last_billed_consumption,
    ),
    REDescription(
        key="intreruperi",
        icon="mdi:transmission-tower-off",
        value_fn=_outage_state,
        attrs_fn=lambda d: {"cod_verificare": (d.get("outage") or {}).get("check")},
    ),
    REDescription(
        key="putere_aprobata",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: to_float(d["info"].get("Absorbed_Power_KW__c")),
        attrs_fn=lambda d: {
            "adresa": d["info"].get("POD_Address__c")
            or ", ".join(
                x for x in (d["info"].get("Description_Street__c"), d["info"].get("City__c")) if x
            ),
            "nivel_tensiune": d["info"].get("Voltage_Level__c"),
            "tarif": d["info"].get("TARIFF__c"),
            "stare_contract": d["info"].get("Contract_State__c"),
            "valabilitate_contract": d["info"].get("Validity_of_the_supply_contract__c"),
            "smart_meter": d["info"].get("Smart_meter__c"),
            "prosumator": d["info"].get("isProductor__c"),
        },
    ),
    REDescription(
        key="consum_azi",
        smart_only=True,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_history_value("today"),
        attrs_fn=_history_attrs("today"),
    ),
    REDescription(
        key="consum_ieri",
        smart_only=True,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_history_value("yesterday"),
        attrs_fn=_history_attrs("yesterday"),
    ),
    REDescription(
        key="consum_luna_curenta",
        smart_only=True,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=_history_value("month"),
        attrs_fn=_history_attrs("month"),
    ),
    REDescription(
        key="valori_instantanee",
        smart_only=True,
        icon="mdi:flash",
        value_fn=_instant_state,
        attrs_fn=lambda d: {"valori": d.get("instant")},
    ),
    REDescription(
        key="stare_contor",
        smart_only=True,
        icon="mdi:meter-electric",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: (d.get("meter") or {}).get("METER_STATUS"),
        attrs_fn=lambda d: {
            "serie": (d.get("meter") or {}).get("METER"),
            "tip": (d.get("meter") or {}).get("METER_TYPE"),
            "montaj": (d.get("meter") or {}).get("METER_FAZIC"),
            "data_instalare": (d.get("meter") or {}).get("INSTALLATION_DATE"),
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ReteleElectriceConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities = []
    for pod, data in (coordinator.data or {}).items():
        for desc in SENSORS:
            if desc.smart_only and not data.get("smart"):
                continue
            entities.append(ReteleElectriceSensor(coordinator, pod, desc))
    async_add_entities(entities)


class ReteleElectriceSensor(ReteleElectriceEntity, SensorEntity):
    entity_description: REDescription

    def __init__(self, coordinator, pod: str, description: REDescription) -> None:
        super().__init__(coordinator, pod, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        try:
            value = self.entity_description.value_fn(self.pod_data)
        except (KeyError, TypeError, ValueError):
            return None
        if isinstance(value, date) or value is None or isinstance(value, (int, float, str)):
            return value
        return str(value)

    @property
    def extra_state_attributes(self) -> Data | None:
        if not self.entity_description.attrs_fn:
            return None
        try:
            return self.entity_description.attrs_fn(self.pod_data)
        except (KeyError, TypeError, ValueError):
            return None
