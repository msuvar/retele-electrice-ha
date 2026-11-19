"""Coordonatorul de date pentru Rețele Electrice România."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ReteleElectriceAuthError, ReteleElectriceClient, ReteleElectriceError
from .const import CONF_PODS, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


def to_float(value: Any) -> float | None:
    """Convertește valorile portalului; „-9999” și șirul gol înseamnă lipsă."""
    if value in (None, "", "-9999", -9999):
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


# Coduri posibile pentru energia activă produsă/livrată în rețea în arhiva de
# indexuri. Portalul folosește „EA” pentru consum; pentru producție codul nu a
# putut fi observat pe un cont fără prosumator, așa că acceptăm variantele uzuale.
PRODUCED_CODES = ("EAP", "EA_P", "EAPR", "EAL", "EAE", "EA-", "EAI")


def _parse_reading_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d.%m.%Y")
    except ValueError:
        return None


class ReteleElectriceCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Actualizează periodic datele pentru POD-urile selectate."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: ReteleElectriceClient
    ) -> None:
        interval = entry.options.get(
            CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=int(interval)),
        )
        self.entry = entry
        self.client = client
        self._meter_info_cache: dict[str, tuple[datetime, dict[str, Any] | None]] = {}
        self._supplier_cache: dict[str, tuple[datetime, dict[str, Any] | None]] = {}
        # Ultima solicitare de citire nouă trimisă din Home Assistant, per POD.
        self.instant_requests: dict[str, dict[str, Any]] = {}

    @property
    def selected_pods(self) -> list[str]:
        return list(self.entry.options.get(CONF_PODS, self.entry.data.get(CONF_PODS, [])))

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            all_pods = await self.client.async_get_pods()
        except ReteleElectriceAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ReteleElectriceError as err:
            raise UpdateFailed(str(err)) from err

        by_name = {p.get("POD__c") or p.get("Name"): p for p in all_pods}
        selected = self.selected_pods or list(by_name)
        result: dict[str, dict[str, Any]] = {}

        for pod in selected:
            info = by_name.get(pod)
            if info is None:
                _LOGGER.warning("POD-ul %s nu mai apare în cont", pod)
                continue
            try:
                result[pod] = await self._update_pod(pod, info)
            except ReteleElectriceAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err

        if not result:
            raise UpdateFailed("Nu am putut citi niciun POD")
        return result

    async def _safe(self, label: str, coro) -> Any:
        try:
            return await coro
        except ReteleElectriceAuthError:
            raise
        except ReteleElectriceError as err:
            _LOGGER.warning("%s: %s", label, err)
            return None

    async def _update_pod(self, pod: str, info: dict[str, Any]) -> dict[str, Any]:
        now = dt_util.now().replace(tzinfo=None)
        code = str(info.get("Fiscal_Code__c") or "")
        smart = bool(info.get("Smart_meter__c"))
        data: dict[str, Any] = {"info": info, "smart": smart}

        # Arhivă indexuri: anul trecut + anul curent (maxim 2 ani), câte o cerere pe an
        raw_readings: list[dict[str, Any]] = []
        for year in (now.year - 1, now.year):
            chunk = await self._safe(
                f"{pod} indexuri {year}",
                self.client.async_get_readings(
                    pod, code, datetime(year, 1, 1), datetime(year + 1, 1, 1)
                ),
            )
            raw_readings.extend(chunk or [])
        data["readings"] = self._normalize_readings(raw_readings)
        data["archive"] = build_archive(data["readings"])

        cached_sup = self._supplier_cache.get(pod)
        if cached_sup is None or now - cached_sup[0] > timedelta(hours=24):
            supplier = await self._safe(
                f"{pod} furnizor", self.client.async_query_pod(pod)
            )
            if supplier is not None or cached_sup is None:
                self._supplier_cache[pod] = (now, supplier)
        data["supplier"] = self._supplier_cache[pod][1]

        data["outage"] = await self._safe(
            f"{pod} întreruperi", self.client.async_get_outage(pod)
        )

        if smart and code:
            cached = self._meter_info_cache.get(pod)
            if cached is None or now - cached[0] > timedelta(hours=24):
                meter = await self._safe(
                    f"{pod} contor", self.client.async_get_meter_info(pod, code)
                )
                self._meter_info_cache[pod] = (now, meter)
            data["meter"] = self._meter_info_cache[pod][1]

            today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
            yday0 = today0 - timedelta(days=1)
            month0 = today0.replace(day=1)
            data["today"] = await self._safe(
                f"{pod} consum azi",
                self.client.async_get_meter_history(pod, code, today0, now),
            )
            data["yesterday"] = await self._safe(
                f"{pod} consum ieri",
                self.client.async_get_meter_history(
                    pod, code, yday0, today0 - timedelta(minutes=1)
                ),
            )
            data["month"] = await self._safe(
                f"{pod} consum lună",
                self.client.async_get_meter_history(pod, code, month0, now),
            )
            data["instant"] = await self._safe(
                f"{pod} valori instantanee",
                self.client.async_get_meter_instant(pod, code),
            )
        return data

    @staticmethod
    def _normalize_readings(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for r in raw:
            date = _parse_reading_date(r.get("measureDate"))
            if date is None:
                continue
            values: dict[str, float] = {}
            for m in r.get("meter") or []:
                kind = str(m.get("typeofenergy_measured") or "").strip().upper()
                val = to_float(m.get("Value"))
                if kind and val is not None:
                    values[kind] = val
            consumed = values.get("EA")
            produced = next(
                (values[k] for k in PRODUCED_CODES if k in values), None
            )
            if consumed is None and produced is None:
                continue
            out.append(
                {
                    "date": date,
                    "value": consumed,
                    "produced": produced,
                    "all": values,
                    "type": r.get("typeOfReading"),
                    "serial": r.get("SerialNumber"),
                }
            )
        unique = {(r["date"], r["type"]): r for r in out}
        return sorted(unique.values(), key=lambda x: x["date"], reverse=True)


def is_prosumer(data: dict[str, Any]) -> bool:
    """POD de prosumator: marcat în cont sau cu date de producție în răspunsuri."""
    if (data.get("info") or {}).get("isProductor__c"):
        return True
    if any(r.get("produced") is not None for r in data.get("readings") or []):
        return True
    return any(
        to_float((data.get(k) or {}).get("SUM_EAP")) is not None
        for k in ("today", "yesterday", "month")
    )


def build_archive(readings: list[dict[str, Any]]) -> dict[str, dict[int, dict[str, Any]]]:
    """Grupează citirile pe ani și calculează totalul anual.

    Totalul unui an = ultimul index din an minus ultimul index din anul anterior
    (sau minus primul index din an, dacă anul anterior lipsește).
    """
    archive: dict[str, dict[int, dict[str, Any]]] = {}
    for field, key in (("value", "consum"), ("produced", "productie")):
        rows = sorted(
            (r for r in readings if r.get(field) is not None), key=lambda r: r["date"]
        )
        by_year: dict[int, list[dict[str, Any]]] = {}
        for r in rows:
            by_year.setdefault(r["date"].year, []).append(r)
        result: dict[int, dict[str, Any]] = {}
        prev_last: float | None = None
        for year in sorted(by_year):
            items = by_year[year]
            base = prev_last if prev_last is not None else items[0][field]
            total = round(items[-1][field] - base, 3)
            monthly: dict[str, float] = {}
            last_val = base
            for r in items:
                if r.get("type") == "real" or r is items[-1]:
                    label = r["date"].strftime("%Y-%m-%d")
                    monthly[label] = round(r[field] - last_val, 3)
                    last_val = r[field]
            result[year] = {
                "total_kwh": total,
                "complet": prev_last is not None,
                "citiri": {r["date"].strftime("%Y-%m-%d"): r[field] for r in items},
                "consum_intre_citiri" if key == "consum" else "productie_intre_citiri": monthly,
            }
            prev_last = items[-1][field]
        archive[key] = result
    return archive
