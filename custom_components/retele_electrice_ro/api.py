"""Client asincron pentru portalul contulmeu.reteleelectrice.ro.

Portalul rulează pe Salesforce Experience Cloud. Datele vin pe două căi:
  * acțiuni Aura (Apex) — lista de POD-uri și datele contului;
  * pagini Visualforce „proxy” (formular A4J cu ViewState) — indexuri,
    întreruperi și datele contorului inteligent.

Clientul imită exact ce face browserul: autentificare prin formularul de
login al site-ului, apoi cereri cu aceleași cookie-uri de sesiune.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import aiohttp

from .const import (
    AURA_GET_PODS,
    AURA_GET_USER_NAME,
    BASE_URL,
    VF_METER_HISTORY,
    VF_METER_INFO,
    VF_METER_INSTANT,
    VF_OUTAGES,
    VF_QUERY_POD,
    VF_READINGS,
)

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=60)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "ro-RO,ro;q=0.9,en;q=0.8",
}

_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([\w:.\-]+)\s*=\s*"([^"]*)"')
_A4J_PARAM_RE = re.compile(r"'parameters':\{'([^']+)':'([^']+)'\}")
_ASYNC_RESP_RE = re.compile(
    r'<span id="[^"]*asyncResponse">(.*?)</span>', re.DOTALL
)
_REDIRECT_RES = (
    re.compile(r"window\.location\.(?:href|replace)\s*[=(]\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"location\.(?:href|replace)\s*[=(]\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"handleRedirect\(\s*['\"]([^'\"]+)['\"]"),
    re.compile(
        r"<meta[^>]+http-equiv=[\"']?refresh[\"']?[^>]+url=([^\"'>]+)",
        re.IGNORECASE,
    ),
)
_LOGIN_PAGE = "PEDRO_SiteLogin"
_JSF_SUBMIT_RE = re.compile(r"jsfcljs\(document\.forms\['[^']+'\],\s*'([^']+)'")
_VF_MSG_RE = re.compile(
    r'<(?:div|span|ul)[^>]+class="[^"]*(?:messageText|errorM\d|message|error)[^"]*"[^>]*>(.*?)</(?:div|span|ul)>',
    re.IGNORECASE | re.DOTALL,
)
_LOGIN_ERROR_RE = re.compile(
    r'<div[^>]+id="error"[^>]*>(.*?)</div>', re.IGNORECASE | re.DOTALL
)


class ReteleElectriceError(Exception):
    """Eroare generică de comunicare cu portalul."""


class ReteleElectriceAuthError(ReteleElectriceError):
    """Autentificare eșuată (credențiale greșite sau pas suplimentar cerut)."""


class _SessionExpired(ReteleElectriceError):
    """Sesiunea Salesforce a expirat; se reface login-ul."""


def _unescape_tree(value: Any) -> Any:
    """Decodează entitățile HTML rămase în valorile de tip text."""
    if isinstance(value, str):
        return html.unescape(value)
    if isinstance(value, list):
        return [_unescape_tree(v) for v in value]
    if isinstance(value, dict):
        return {k: _unescape_tree(v) for k, v in value.items()}
    return value


def _parse_inputs(page: str) -> list[tuple[str, str]]:
    """Extrage perechile (name, value) din toate tagurile <input>."""
    fields: list[tuple[str, str]] = []
    for tag in _INPUT_RE.findall(page):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag)}
        name = attrs.get("name")
        if name:
            fields.append((name, html.unescape(attrs.get("value", ""))))
    return fields


def _strip_meta(value: Any) -> Any:
    """Elimină câmpurile tehnice SOAP (…_type_info, field_order, apex_schema)."""
    if isinstance(value, list):
        return [_strip_meta(v) for v in value]
    if isinstance(value, dict):
        return {
            k: _strip_meta(v)
            for k, v in value.items()
            if not k.endswith("_type_info") and k not in ("apex_schema_type_info",)
        }
    return value


def _is_login(text: str) -> bool:
    """Adevărat dacă URL-ul/textul indică pagina de login (sesiune expirată)."""
    low = text.lower()
    return "/login" in low or "sitelogin" in low or "/s/login" in low


def _fmt(dt: datetime) -> str:
    """Formatul de dată așteptat de serviciile portalului."""
    return dt.strftime("%d/%m/%Y %H:%M:%S")


class ReteleElectriceClient:
    """Client pentru un singur cont de pe portal."""

    def __init__(
        self, session: aiohttp.ClientSession, username: str, password: str
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._lock = asyncio.Lock()
        self._logged_in = False
        self._aura_context: dict[str, Any] | None = None
        self._aura_token: str | None = None
        self._aura_counter = 0

    # ------------------------------------------------------------------ #
    # Autentificare
    # ------------------------------------------------------------------ #
    async def _request(self, method: str, url: str, **kwargs: Any) -> tuple[str, str]:
        """Execută o cerere și întoarce (url_final, corp)."""
        try:
            async with self._session.request(
                method, url, headers=_HEADERS, timeout=_TIMEOUT, **kwargs
            ) as resp:
                text = await resp.text(errors="replace")
                if resp.status >= 500:
                    raise ReteleElectriceError(f"HTTP {resp.status} la {url}")
                return str(resp.url), text
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ReteleElectriceError(f"Eroare de rețea: {err}") from err

    async def login(self) -> None:
        """Autentificare prin formularul de login al site-ului Salesforce."""
        self._logged_in = False
        self._aura_context = None
        self._aura_token = None
        self._session.cookie_jar.clear()

        url, text = await self._site_login()
        _LOGGER.debug("Login: răspuns de la %s (%d caractere)", url.split("?")[0], len(text))

        # Salesforce răspunde de obicei cu o pagină care face redirect prin JS
        # către frontdoor.jsp, unde se setează cookie-ul de sesiune.
        for _ in range(6):
            target = None
            for rx in _REDIRECT_RES:
                if m := rx.search(text):
                    target = html.unescape(m.group(1)).strip()
                    break
            if not target or target.startswith("javascript"):
                break
            _LOGGER.debug("Login: redirect către %s", target.split("?")[0])
            url, text = await self._request("GET", urljoin(url, target))

        if err := _LOGIN_ERROR_RE.search(text):
            msg = re.sub(r"<[^>]+>", "", err.group(1)).strip()
            if msg:
                raise ReteleElectriceAuthError(msg)
        if _LOGIN_PAGE in url and 'type="password"' in text:
            # Am rămas pe pagina de login: afișăm mesajul portalului, dacă există.
            msg = ""
            if m := _VF_MSG_RE.search(text):
                msg = re.sub(r"<[^>]+>", " ", m.group(1))
                msg = re.sub(r"\s+", " ", html.unescape(msg)).strip()
            raise ReteleElectriceAuthError(msg or "Email sau parolă incorectă")
        if "/_ui/identity/verification" in url or "verification" in url.lower():
            raise ReteleElectriceAuthError(
                "Portalul cere verificare suplimentară (cod pe email/SMS). "
                "Autentifică-te o dată în browser și încearcă din nou."
            )

        await self._bootstrap_aura()
        self._logged_in = True
        _LOGGER.debug("Autentificare reușită pentru %s", self._username)

    async def _site_login(self) -> tuple[str, str]:
        """Trimite formularul Visualforce PEDRO_SiteLogin (pagina de login a portalului)."""
        url, page = await self._request(
            "GET", f"{BASE_URL}/{_LOGIN_PAGE}?startURL=%2Fs%2F"
        )
        form_tag = re.search(r"<form\b[^>]*>", page, re.IGNORECASE)
        fields = _parse_inputs(page)
        user_field = next((n for n, _ in fields if n.endswith(":username")), None)
        pass_field = next((n for n, _ in fields if n.endswith(":password")), None)
        if not form_tag or not user_field or not pass_field:
            raise ReteleElectriceError("Nu am găsit formularul de login al portalului")

        action = dict(_ATTR_RE.findall(form_tag.group(0))).get("action") or url
        data: list[tuple[str, str]] = []
        for name, value in fields:
            if name == user_field:
                value = self._username
            elif name == pass_field:
                value = self._password
            data.append((name, value))
        # Butonul „Autentifică-te” trimite și id-ul componentei (ex. loginForm:j_id25).
        if m := _JSF_SUBMIT_RE.search(page):
            parts = m.group(1).split(",")
            data.extend(zip(parts[0::2], parts[1::2]))

        return await self._request(
            "POST", urljoin(url, html.unescape(action)), data=data
        )

    async def _bootstrap_aura(self) -> None:
        """Citește contextul Aura (fwuid, versiune app, token) din pagina /s/."""
        url, page = await self._request("GET", f"{BASE_URL}/s/")
        if _is_login(url):
            raise ReteleElectriceAuthError("Email sau parolă incorectă")

        fwuid = re.search(r'"fwuid":"([^"]+)"', page)
        loaded = re.search(r'"loaded":(\{[^}]*\})', page)
        if not fwuid or not loaded:
            raise ReteleElectriceError("Nu am găsit contextul Aura în pagina /s/")
        self._aura_context = {
            "mode": "PROD",
            "fwuid": fwuid.group(1),
            "app": "siteforce:communityApp",
            "loaded": json.loads(loaded.group(1)),
            "dn": [],
            "globals": {},
            "uad": True,
        }

        # Tokenul CSRF Aura este livrat în bootstrap.js, la fel ca în browser.
        token = None
        if bs := re.search(r'<script src="([^"]*bootstrap\.js[^"]*)"', page):
            _, js = await self._request(
                "GET", urljoin(BASE_URL, html.unescape(bs.group(1)))
            )
            if m := re.search(r'"token"\s*:\s*"([^"]+)"', js):
                token = m.group(1)
        if token is None:
            for cookie in self._session.cookie_jar:
                if cookie.key.startswith("__Host-ERIC_"):
                    token = cookie.value
                    break
        self._aura_token = token or "undefined"

    async def _ensure_login(self) -> None:
        if not self._logged_in:
            await self.login()

    # ------------------------------------------------------------------ #
    # Aura
    # ------------------------------------------------------------------ #
    async def _aura_once(self, descriptor: str, params: dict[str, Any]) -> Any:
        assert self._aura_context is not None
        self._aura_counter += 1
        message = {
            "actions": [
                {
                    "id": f"{self._aura_counter};a",
                    "descriptor": descriptor,
                    "callingDescriptor": "UNKNOWN",
                    "params": params,
                }
            ]
        }
        data = {
            "message": json.dumps(message),
            "aura.context": json.dumps(self._aura_context),
            "aura.pageURI": "/s/",
            "aura.token": self._aura_token or "undefined",
        }
        _, text = await self._request(
            "POST",
            f"{BASE_URL}/s/sfsites/aura?r={self._aura_counter}&other.ha=1",
            data=data,
        )
        body = text.strip()
        if body.startswith("while(1);"):
            body = body[len("while(1);") :]
        body = body.lstrip("*/").strip()
        if "aura:invalidSession" in body or "aura:clientOutOfSync" in body:
            raise _SessionExpired(body[:200])
        try:
            payload = json.loads(body)
        except ValueError as err:
            if _is_login(body[:2000]):
                raise _SessionExpired("redirect la login") from err
            raise ReteleElectriceError(f"Răspuns Aura invalid: {body[:200]}") from err
        action = payload["actions"][0]
        if action.get("state") != "SUCCESS":
            raise ReteleElectriceError(
                f"Acțiunea {descriptor} a eșuat: {action.get('error')}"
            )
        return action.get("returnValue")

    async def _aura(self, descriptor: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        async with self._lock:
            await self._ensure_login()
            try:
                return await self._aura_once(descriptor, params)
            except _SessionExpired:
                _LOGGER.debug("Sesiune expirată (Aura); refac autentificarea")
                await self.login()
                return await self._aura_once(descriptor, params)

    # ------------------------------------------------------------------ #
    # Visualforce proxy
    # ------------------------------------------------------------------ #
    async def _vf_once(self, page: str, method: str, params: str) -> Any:
        url, form_page = await self._request("GET", f"{BASE_URL}/{page}")
        if _is_login(url) or "com.salesforce.visualforce.ViewState" not in form_page:
            raise _SessionExpired(f"{page} nu a întors formularul")

        fields = _parse_inputs(form_page)
        data: list[tuple[str, str]] = [("AJAXREQUEST", "_viewRoot")]
        for name, value in fields:
            if name == "methodN":
                value = method
            elif name == "params":
                value = params
            elif name == "uniqueId":
                value = str(uuid.uuid4())
            data.append((name, value))
        if m := _A4J_PARAM_RE.search(form_page):
            data.append((m.group(1), m.group(2)))

        _, text = await self._request("POST", f"{BASE_URL}/{page}", data=data)
        m = _ASYNC_RESP_RE.search(text)
        if not m:
            if _is_login(text[:3000]):
                raise _SessionExpired(f"{page}: redirect la login")
            raise ReteleElectriceError(f"{page}/{method}: răspuns fără date")
        raw = html.unescape(m.group(1)).strip()
        if not raw:
            return None
        try:
            return _unescape_tree(json.loads(raw))
        except ValueError as err:
            raise ReteleElectriceError(
                f"{page}/{method}: JSON invalid: {raw[:200]}"
            ) from err

    async def _vf(self, page: str, method: str, params: str) -> Any:
        async with self._lock:
            await self._ensure_login()
            try:
                return await self._vf_once(page, method, params)
            except _SessionExpired:
                _LOGGER.debug("Sesiune expirată (VF); refac autentificarea")
                await self.login()
                return await self._vf_once(page, method, params)

    # ------------------------------------------------------------------ #
    # API public
    # ------------------------------------------------------------------ #
    async def async_get_user_name(self) -> str:
        name = await self._aura(AURA_GET_USER_NAME)
        return str(name or "").strip()

    async def async_get_pods(self) -> list[dict[str, Any]]:
        pods = await self._aura(AURA_GET_PODS)
        return list(pods or [])

    async def async_query_pod(self, pod: str) -> dict[str, Any] | None:
        """Date furnizor, contract și contor pentru POD (ca în „Informații POD”)."""
        res = await self._vf(VF_QUERY_POD, "queryPOD", f"{pod},Client_Company")
        if not isinstance(res, dict) or not res:
            return None
        res = _strip_meta(res)
        meters = res.get("Contor") or []
        if isinstance(meters, dict):
            meters = [meters]
        res["Contor"] = meters
        return res

    async def async_get_outage(self, pod: str) -> dict[str, Any]:
        res = await self._vf(VF_OUTAGES, "PowerOutages", f"{pod},ro") or {}
        data = res.get("data") or {}
        return {
            "message": (data.get("messaggio") or res.get("message") or "").strip(),
            "check": data.get("checkInterruzione"),
            "result": res.get("esito"),
        }

    async def async_get_readings(
        self, pod: str, customer_code: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        params = f",,{customer_code},{pod},{_fmt(start)},{_fmt(end)}"
        res = await self._vf(VF_READINGS, "RetriveSingleSelf", params) or {}
        if res.get("Result") != "OK":
            _LOGGER.debug("Indexuri indisponibile pentru %s: %s", pod, res)
            return []
        return list(res.get("XML_Readings") or [])

    async def async_get_meter_info(
        self, pod: str, customer_code: str
    ) -> dict[str, Any] | None:
        res = await self._vf(
            VF_METER_INFO, "FindOutMeterCurrentInfo", f"{customer_code},,{pod}"
        ) or {}
        return res.get("row") if res.get("Result") == "OK" else None

    async def async_get_meter_history(
        self, pod: str, customer_code: str, start: datetime, end: datetime
    ) -> dict[str, Any] | None:
        """Totaluri din curba de sarcină pe intervalul dat (SUM_EA în kWh)."""
        params = f"{customer_code},,{pod},{_fmt(start)},{_fmt(end)}"
        res = await self._vf(VF_METER_HISTORY, "FindOutMeterHistoryData", params) or {}
        if res.get("Result") != "OK":
            return None
        rows = res.get("row") or []
        if isinstance(rows, dict):
            rows = [rows]
        return rows[0] if rows else None

    async def async_get_meter_instant(
        self, pod: str, customer_code: str
    ) -> list[dict[str, Any]] | None:
        """Ultimele valori instantanee (disponibile doar după o solicitare din portal)."""
        res = await self._vf(
            VF_METER_INSTANT, "FindOutMeterInstantData", f"{customer_code},,{pod}"
        ) or {}
        if res.get("Result") != "OK":
            return None
        values = res.get("dataIstantValueList")
        if isinstance(values, dict):
            values = [values]
        return values or None
