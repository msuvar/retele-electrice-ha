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
from .coordinator import is_prosumer, to_float
from .entity import ReteleElectriceEntity

Data = dict[str, Any]


def _with(d: Data, field: str) -> list[Data]:
    return [r for r in d.get("readings") or [] if r.get(field) is not None]


def _latest(d: Data, field: str = "value") -> Data | None:
    r = _with(d, field)
    return r[0] if r else None


def _last_month(field: str) -> Callable[[Data], float | None]:
    """Diferența dintre ultimele două citiri reale (de obicei pe 1 ale lunii)."""

    def fn(d: Data) -> float | None:
        real = [r for r in _with(d, field) if r.get("type") == "real"]
        if len(real) < 2:
            return None
        return round(real[0][field] - real[1][field], 3)

    return fn


def _readings_attrs(field: str) -> Callable[[Data], Data]:
    def fn(d: Data) -> Data:
        latest = _latest(d, field)
        if not latest:
            return {}
        return {
            "data_citire": latest["date"].date().isoformat(),
            "tip_citire": latest["type"],
            "serie_contor": latest["serial"],
            "coduri_energie": sorted(latest.get("all", {})),
            "istoric": {
                r["date"].date().isoformat(): r[field] for r in _with(d, field)[:24]
            },
        }

    return fn


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


def _supplier_attrs(d: Data) -> Data:
    sup = d.get("supplier") or {}
    if not sup:
        return {}
    keys = (
        "furnizor_pre", "cui", "activ", "activ_furnizor_la", "activ_consumator_la",
        "kw_aprobata", "kw_evacuata", "delimitare", "u_delimitare", "racordare",
        "telecitit", "aggregation_formula", "compensation_mode", "cer_version",
        "cer_date", "atr_number", "atr_date", "cft_description", "cft_ou_description",
    )
    attrs: Data = {k: sup.get(k) for k in keys if sup.get(k) not in (None, "", " - ")}
    attrs["contoare"] = [
        {
            k: m.get(k)
            for k in ("seria", "marca", "det_tip", "data_montare", "constanta",
                      "precizie", "ee_orara", "kw_interval", "tipmontaj_cod")
            if m.get(k) not in (None, "")
        }
        for m in sup.get("Contor") or []
    ]
    return attrs


def _archive_value(kind: str) -> Callable[[Data], float | None]:
    """Totalul anului curent (sau al celui mai recent an cu citiri)."""

    def fn(d: Data) -> float | None:
        years = (d.get("archive") or {}).get(kind) or {}
        if not years:
            return None
        return years[max(years)]["total_kwh"]

    return fn


def _archive_attrs(kind: str) -> Callable[[Data], Data]:
    def fn(d: Data) -> Data:
        years = (d.get("archive") or {}).get(kind) or {}
        attrs: Data = {"an": max(years) if years else None}
        for year, info in sorted(years.items(), reverse=True):
            attrs[str(year)] = info
        return attrs

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
    prosumer_only: bool = False
    uses_requests: bool = False


SENSORS: tuple[REDescription, ...] = (
    REDescription(
        key="index_energie",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=0,
        value_fn=lambda d: (_latest(d) or {}).get("value"),
        attrs_fn=_readings_attrs("value"),
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
        value_fn=_last_month("value"),
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
            "tip_contract": d["info"].get("Contract_Type__c"),
            "tip_consumator": d["info"].get("Consumer_Type_Account__c"),
            "profil_consum": d["info"].get("ConsumptionProfile__c"),
            "piata": d["info"].get("Market_Type__c"),
            "putere_absorbita_kva": to_float(d["info"].get("Absorbed_Power_KVA__c")),
            "putere_cedata_kw": to_float((d.get("supplier") or {}).get("kw_evacuata")),
            "serie_contor": d["info"].get("EA_METER_SERIE__c"),
            "tip_contor": d["info"].get("EA_METER_TYPE__c"),
            "regim_lucru": d["info"].get("Working_Mode__c"),
        },
    ),
    REDescription(
        key="furnizor",
        icon="mdi:office-building",
        value_fn=lambda d: (d.get("supplier") or {}).get("furnizor"),
        attrs_fn=_supplier_attrs,
    ),
    REDescription(
        key="arhiva_consum",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=0,
        icon="mdi:archive-outline",
        value_fn=_archive_value("consum"),
        attrs_fn=_archive_attrs("consum"),
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
        uses_requests=True,
    ),
    REDescription(
        key="index_energie_produsa",
        prosumer_only=True,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=0,
        icon="mdi:solar-power",
        value_fn=lambda d: (_latest(d, "produced") or {}).get("produced"),
        attrs_fn=_readings_attrs("produced"),
    ),
    REDescription(
        key="arhiva_productie",
        prosumer_only=True,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=0,
        icon="mdi:archive-outline",
        value_fn=_archive_value("productie"),
        attrs_fn=_archive_attrs("productie"),
    ),
    REDescription(
        key="productie_ultima_luna",
        prosumer_only=True,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=0,
        icon="mdi:solar-power",
        value_fn=_last_month("produced"),
    ),
    REDescription(
        key="productie_azi",
        smart_only=True,
        prosumer_only=True,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        icon="mdi:solar-power",
        value_fn=_history_value("today", "SUM_EAP"),
    ),
    REDescription(
        key="productie_ieri",
        smart_only=True,
        prosumer_only=True,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        icon="mdi:solar-power",
        value_fn=_history_value("yesterday", "SUM_EAP"),
    ),
    REDescription(
        key="productie_luna_curenta",
        smart_only=True,
        prosumer_only=True,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        icon="mdi:solar-power",
        value_fn=_history_value("month", "SUM_EAP"),
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
            if desc.prosumer_only and not is_prosumer(data):
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
            attrs = self.entity_description.attrs_fn(self.pod_data)
        except (KeyError, TypeError, ValueError):
            return None
        if self.entity_description.uses_requests:
            attrs = {**attrs, **self.coordinator.instant_requests.get(self.pod, {})}
        return attrs
