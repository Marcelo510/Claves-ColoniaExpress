# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import json
import time
import random
import string
import threading
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor

import requests
from playwright.sync_api import sync_playwright

from buquebus_clientV2 import BuquebusClientV2
from buquebus_client import log, DEFAULT_TIMEOUT


class BuquebusClientUYU(BuquebusClientV2):

    BQB_BASE = "https://www.buquebus.com"
    PRODUCT_HTML = f"{BQB_BASE}/uy/product"
    API_PRICE_AVAIL = f"{BQB_BASE}/api/priceAvailability"

    BASE_DIR = Path(__file__).resolve().parent
    CACHE_TOKEN_FILE = BASE_DIR / "token_cache_uy.json"
    STORAGE_STATE_FILE = BASE_DIR / "storage_state_uy.json"

    _token_lock = threading.Lock()

    def __init__(self, headless: bool = True, timeout_ms: int = DEFAULT_TIMEOUT):
        super().__init__(headless=headless, timeout_ms=timeout_ms)
        self._session = requests.Session()

    # -------------------- utils --------------------

    def _rand_rsc(self, n=5):
        return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))

    def _now_ts(self):
        return int(time.time())

    # -------------------- token cache --------------------

    def _get_cached_token(self):
        if self.CACHE_TOKEN_FILE.exists():
            try:
                data = json.loads(self.CACHE_TOKEN_FILE.read_text("utf-8"))
                if data.get("token") and data.get("exp", 0) > self._now_ts():
                    return data["token"]
            except:
                pass
        return None

    def _save_token(self, token, exp):
        try:
            self.CACHE_TOKEN_FILE.write_text(
                json.dumps({"token": token, "exp": exp}),
                encoding="utf-8"
            )
        except:
            pass

    def _get_valid_token(self):

        tok = self._get_cached_token()
        if tok:
            return tok

        with self._token_lock:

            tok = self._get_cached_token()
            if tok:
                return tok

            tok, exp = self._obtain_token_via_playwright()
            if not tok:
                raise RuntimeError("No se pudo obtener token automáticamente para /uy.")

            self._save_token(tok, exp)
            return tok

    # -------------------- token capture --------------------

    def _obtain_token_via_playwright(self):

        log("UY: capturando token desde navegador...")

        holder = {"tok": None}

        def on_request(req):
            try:
                if "/api/" in req.url and req.post_data:
                    j = json.loads(req.post_data)
                    tok = j.get("token")
                    if tok:
                        holder["tok"] = tok
            except:
                pass

        with sync_playwright() as p:

            browser = p.chromium.launch(headless=self.headless)

            context = browser.new_context(
                storage_state=str(self.STORAGE_STATE_FILE)
                if self.STORAGE_STATE_FILE.exists() else None
            )

            page = context.new_page()
            page.on("request", on_request)

            url = f"{self.PRODUCT_HTML}?_rsc={self._rand_rsc()}"
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)

            page.wait_for_timeout(2000)

            # 🔥 FORZAR llamada igual al frontend
            try:
                page.evaluate("""
                async () => {
                    await fetch("/api/products", {
                        method: "POST",
                        headers: {"content-type":"application/json"},
                        body: JSON.stringify({productType:"PASSENGER"})
                    }).catch(()=>{});
                }
                """)
            except:
                pass

            page.wait_for_timeout(4000)

            # fallback buscar token en HTML
            if not holder["tok"]:
                try:
                    html = page.content()
                    m = re.search(r'"token"\s*:\s*"([A-Za-z0-9-_\.]+)"', html)
                    if m:
                        holder["tok"] = m.group(1)
                except:
                    pass

            try:
                context.storage_state(path=str(self.STORAGE_STATE_FILE))
            except:
                pass

            browser.close()

        if holder["tok"]:
            return holder["tok"], self._now_ts() + 36000

        return None, None

    # -------------------- request --------------------

    def _call_price_availability(self, payload):

        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": self.BQB_BASE,
            "referer": self.PRODUCT_HTML
        }

        resp = self._session.post(
            self.API_PRICE_AVAIL,
            json=payload,
            headers=headers,
            timeout=30
        )

        return resp.status_code, resp.json() if resp.content else {}

    # -------------------- payload --------------------

    def _payload(self, origen, destino, fecha, code):

        yy, mm, dd = self._normalize_date_to_yymmdd(fecha)
        yymmdd = yy + mm + dd

        return {
            "token": self._get_valid_token(),
            "request": {
                "m_AGT_AgencyIdentity": {
                    "m_AGAC_AgentsAccountNumber": {"m_AGAC_AgentAccountNumber": "7252"},
                    "c_CURR_CurrencyForTransaction": {
                        "m_6345_CurrencyCoded": "UYU",
                        "c_U428_DecimalPrecision": "2"
                    },
                    "c_CMPNY_Company": {
                        "m_C045_TradingUnitName": "PAX",
                        "m_C046_CompanyName": "BUQUEBUS",
                        "m_C047_GeographycalLocationName": "South America",
                        "m_C048_DivisionName": "BQB"
                    },
                    "c_SCHN_SalesChannel": {"m_C056_SalesChannel": "WEB"}
                },
                "m_RDQ_RouteDateTimeRequest": [{
                    "index": "0",
                    "m_LEGJ_LegOrSectorOfJourney": {"m_LEGJ_LegOrSectorOfJourney": "1"},
                    "m_ROUT_TravelRoute": {
                        "m_U271_ServiceAgreementDeparturePort": origen,
                        "m_U272_ServiceAgreementDestinationPort": destino
                    },
                    "m_DPDT_DepartureDateTime": {
                        "m_U247_StandardDepartureDate": yymmdd,
                        "m_U248_StandardDepartureTime": "0000"
                    },
                    "c_DPDT_DepartureDateTimeTo": {
                        "m_U247_StandardDepartureDate": yymmdd,
                        "m_U248_StandardDepartureTime": "2359"
                    }
                }],
                "c_PAQ_PassengerDetailRequest": [{
                    "index": "0",
                    "m_LEGJ_LegOrSectorOfJourney": {"m_LEGJ_LegOrSectorOfJourney": "1"},
                    "m_PAXS_PassengerSet": [{
                        "passengerIndex": "0",
                        "m_U257_PassengerTypeCode": "ADL",
                        "m_U258_NumberOfPassengers": 1
                    }]
                }],
                "c_ACQ_AccomodationDetailsRequest": [{
                    "m_LEGJ_LegOrSectorOfJourney": {"m_LEGJ_LegOrSectorOfJourney": "1"},
                    "m_ACPL_AccomodationPlaces": {"m_U228_QuantityOfUnits": 1},
                    "m_ACDT_AccomodationDetails": {
                        "m_U220_ServiceAgreementDefinedAccomodationCode": code,
                        "i_U221_AccomodationType": "CHAIR"
                    },
                    "c_IncludeDiscounts": True,
                }],
                "c_VEQ_VehicleRequest": [],
                "c_MTT_MultipleTariffTypeRequest": [{
                    "m_LEGJ_LegOrSectorOfJourney": {"m_LEGJ_LegOrSectorOfJourney": "1"},
                    "m_TARF_TariffCodeTypeDescription": [
                        {"c_U282_TariffType": "PROGRAMADA", "c_C113_PriceDetailRequested": "true"}
                    ]
                }]
            }
        }

    # -------------------- PRO --------------------

    def fetch_day_web_prices_pro(self, origen, destino, fecha):

        # asegurar token antes del paralelo
        self._get_valid_token()

        with ThreadPoolExecutor(max_workers=4) as ex:

            fut_t = ex.submit(self._call_price_availability,
                            self._payload(origen, destino, fecha, "TSEAT"))

            fut_b = ex.submit(self._call_price_availability,
                            self._payload(origen, destino, fecha, "BSEAT"))

            fut_p = ex.submit(self._call_price_availability,
                            self._payload(origen, destino, fecha, "PRSEAT"))

            fut_e = ex.submit(self._call_price_availability,
                            self._payload(origen, destino, fecha, "ESEAT"))

            st_t, raw_t = fut_t.result()
            st_b, raw_b = fut_b.result()
            st_p, raw_p = fut_p.result()
            st_e, raw_e = fut_e.result()

        if st_t != 200:
            return st_t, raw_t

        # ---------- usar mismos extractores que AR ----------

        mapa_t = self._totales_programada_por_sailing(raw_t)
        mapa_b = self._totales_programada_por_sailing(raw_b)
        mapa_p = self._totales_programada_por_sailing(raw_p)
        mapa_e = self._totales_por_tarifa(raw_e, "ECONOMICA") if st_e == 200 else {}

        # ---------- formateo moneda UYU ----------
        def fmt(v):
            if v is None:
                return None
            return f"UYU {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

        results = []

        for code in sorted(set(mapa_t) | set(mapa_b) | set(mapa_p) | set(mapa_e)):

            tur, hs, ha, ship_t = mapa_t.get(code, (None, None, None, None))
            bus, _, _, ship_b = mapa_b.get(code, (None, None, None, None))
            pri, _, _, ship_p = mapa_p.get(code, (None, None, None, None))
            eco, hs_e, ha_e, ship_e = mapa_e.get(code, (None, None, None, None))

            barco = ship_t or ship_b or ship_p or ship_e
            hora_salida = hs or hs_e
            hora_llegada = ha or ha_e

            diff = (bus - tur) if (bus and tur) else None

            results.append({
                "hora_salida": hora_salida,
                "hora_llegada": hora_llegada,
                "barco": barco,
                "codigo": code,

                "turista": fmt(tur),
                "business": fmt(bus),
                "diferencia": fmt(diff),

                "economica": fmt(eco),
                "primera": fmt(pri),
            })

        results.sort(key=lambda x: x["hora_salida"] or "99:99")

        return 200, results
