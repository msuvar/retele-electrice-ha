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

        # Arhivă indexuri: ultimele ~13 luni
        start = (now.replace(day=1) - timedelta(days=380)).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        end = (now.replace(day=28) + timedelta(days=5)).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        readings = await self._safe(
            f"{pod} indexuri", self.client.async_get_readings(pod, code, start, end)
        )
        data["readings"] = self._normalize_readings(readings or [])

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
        out.sort(key=lambda x: x["date"], reverse=True)
        return out


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
