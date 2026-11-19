"""Constante pentru integrarea Rețele Electrice România."""

from __future__ import annotations

DOMAIN = "retele_electrice_ro"
MANUFACTURER = "Rețele Electrice România"

BASE_URL = "https://contulmeu.reteleelectrice.ro"

CONF_PODS = "pods"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_SCAN_INTERVAL = 3600  # secunde
MIN_SCAN_INTERVAL = 900
MAX_SCAN_INTERVAL = 86400

# Pagini Visualforce folosite de portal ca proxy către serviciile interne
VF_OUTAGES = "PED_ProxyCallWSAsynPowerOutages_VF"
VF_READINGS = "PED_ProxyCallWSAsynSingleSelf_VF"
VF_METER_INFO = "PED_ProxyCallWSAsynSmartMeterCurrentData"
VF_METER_HISTORY = "PED_ProxyCallWSAsync_SmartMeter_Vf"
VF_METER_INSTANT = "PED_ProxyCallWSAsynSmartMeterIstantData"

# Acțiuni Aura (Apex) folosite de portal
AURA_GET_PODS = "apex://PED_Utility/ACTION$getPODs"
AURA_GET_USER_NAME = "apex://PED_Utility/ACTION$getUserName"

PLATFORMS = ["sensor", "binary_sensor"]
