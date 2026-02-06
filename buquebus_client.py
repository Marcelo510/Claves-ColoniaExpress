# -*- coding: utf-8 -*-
import os, json, time, random, string, re
from pathlib import Path
from typing import Tuple, Any, Dict, List, Optional

import requests
from playwright.sync_api import sync_playwright

# -------------------- Paths / Constantes --------------------
BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "buquebus_log.txt"

CACHE_TOKEN_FILE = BASE_DIR / "token_cache.json"
STORAGE_STATE_FILE = BASE_DIR / "storage_state.json"

BQB_BASE = "https://www.buquebus.com"
PRODUCT_HTML = f"{BQB_BASE}/ar/product"
API_PRODUCTS = f"{BQB_BASE}/api/products"
API_PRICE_AVAIL = f"{BQB_BASE}/api/priceAvailability"

DEFAULT_TIMEOUT = 30_000  # ms

# -------------------- Utilidades --------------------
def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    LOG_FILE.write_text((LOG_FILE.read_text(encoding="utf-8") if LOG_FILE.exists() else "") + f"[{ts}] {msg}\n", encoding="utf-8")
    print(f"[{ts}] {msg}")

def _now_ts() -> int:
    return int(time.time())

def _rand_rsc(n: int = 5) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))

def _to_money(v) -> Optional[float]:
    try:
        if v is None:
            return None
        if isinstance(v, str) and v.strip().upper() == "N/A":
            return None
        return round(int(str(v)) / 100.0, 2)
    except Exception:
        return None

def _sum_price_details(price_details):
    """
    Suma los valores m_C088_UnitGrossAmount de cada item dentro de c_PRIC_PriceDetails.
    Devuelve un float (por ejemplo 86234.40)
    """
    total = 0.0
    for p in price_details or []:
        val = _to_money(p.get("m_C088_UnitGrossAmount"))
        total += val if val else 0
    return total

# -------------------- Cliente --------------------
class BuquebusClient:
    def __init__(self, headless: bool = True, timeout_ms: int = DEFAULT_TIMEOUT):
        # Permite override por ENV
        env_headless = os.environ.get("BQB_HEADLESS")
        if env_headless is not None:
            headless = str(env_headless).strip().lower() in ("1", "true", "yes", "y", "on")
        self.headless = headless
        self.timeout_ms = timeout_ms
        log(f"Playwright launch headless={self.headless}")

    # ---------- Fecha helpers ----------
    def _normalize_date_to_yymmdd(self, fecha: str) -> Tuple[str, str, str]:
        """
        Admite: YYYY-MM-DD | DD/MM/YYYY | YYMMDD
        Devuelve: (YY, MM, DD) como strings
        """
        fecha = fecha.strip()
        if re.fullmatch(r"\d{6}", fecha):
            return fecha[0:2], fecha[2:4], fecha[4:6]
        if "/" in fecha:
            # DD/MM/YYYY
            dd, mm, yyyy = fecha.split("/")
        else:
            # YYYY-MM-DD
            yyyy, mm, dd = fecha.split("-")
        yy = yyyy[-2:]
        return yy, mm.zfill(2), dd.zfill(2)

    # ---------- Token cache ----------
    def _get_cached_token(self) -> Optional[str]:
        if CACHE_TOKEN_FILE.exists():
            try:
                data = json.loads(CACHE_TOKEN_FILE.read_text(encoding="utf-8"))
                tok = data.get("token")
                exp = data.get("exp")
                if tok and exp and int(exp) > _now_ts():
                    return tok
            except Exception:
                pass
        return None

    def _save_token(self, token: str, exp: Optional[int] = None):
        try:
            CACHE_TOKEN_FILE.write_text(json.dumps({"token": token, "exp": exp}, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _get_valid_token(self) -> str:
        # 1) Token por ENV
        env_tok = os.environ.get("BQB_STATIC_TOKEN")
        if env_tok:
            return env_tok.strip()

        # 2) Cache
        tok = self._get_cached_token()
        if tok:
            return tok

        # 3) Captura automatizada
        tok, exp = self._obtain_token_via_playwright()
        if not tok:
            raise RuntimeError("No se pudo obtener token automáticamente.")
        self._save_token(tok, exp)
        return tok

    # ---------- Playwright: captura token ----------
    def _obtain_token_via_playwright(self) -> Tuple[Optional[str], Optional[int]]:
        log("No hay token cacheado válido. Intentando capturarlo desde el sitio…")

        token_holder = {"tok": None, "exp": None}

        def on_request(req):
            url = req.url
            if "/api/products" in url or "/api/priceAvailability" in url:
                try:
                    body = req.post_data or ""
                    if body:
                        j = json.loads(body)
                        tok = j.get("token")
                        if tok and not token_holder["tok"]:
                            token_holder["tok"] = tok
                            log(f"Token capturado en REQUEST {url} [inline].")
                except Exception:
                    pass

        # Navega a /ar/product?_rsc=xxxxx y escucha XHR
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            context = browser.new_context(storage_state=str(STORAGE_STATE_FILE) if STORAGE_STATE_FILE.exists() else None)
            page = context.new_page()

            page.on("request", on_request)

            # Ruta para debug (no bloquea)
            def _route_debug(route, request):
                route.continue_()
            page.route(re.compile(r".*/api/(products|priceAvailability).*"), _route_debug)

            rsc = _rand_rsc()
            url = f"{PRODUCT_HTML}?_rsc={rsc}"
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            log("Página cargada, buscando token en HTML/__NEXT_DATA__ y escuchando XHR…")

            # Primer pase: esperar un poco por XHR
            page.wait_for_timeout(2500)

            # Si no apareció, forzar alguna interacción que dispare /api/products
            if not token_holder["tok"]:
                try:
                    page.evaluate("""() => {
                        window.fetch && fetch("/api/products", {
                          method: "POST",
                          headers: {"content-type":"application/json"},
                          body: JSON.stringify({productType:"PASSENGER"})
                        }).catch(()=>{});
                    }""")
                except Exception:
                    pass
                page.wait_for_timeout(2000)

            # Plan B: recargar con otro _rsc
            if not token_holder["tok"]:
                log("No hubo token en primer pase; recargando con otro _rsc …")
                rsc2 = _rand_rsc()
                page.goto(f"{PRODUCT_HTML}?_rsc={rsc2}", wait_until="domcontentloaded", timeout=self.timeout_ms)
                page.wait_for_timeout(2500)

            # Si todavía no, último intento: parsear __NEXT_DATA__ (a veces aparece)
            if not token_holder["tok"]:
                try:
                    html = page.content()
                    # Por si apareciera embebido
                    m = re.search(r'"token"\s*:\s*"([A-Za-z0-9-_\.]+)"', html)
                    if m:
                        token_holder["tok"] = m.group(1)
                except Exception:
                    pass

            # Guardar storage (cookies) para próximas corridas
            try:
                context.storage_state(path=str(STORAGE_STATE_FILE))
            except Exception:
                pass

            browser.close()

        if token_holder["tok"]:
            # El exp no viene directo; lo marcamos +10h para reuso práctico
            approx_exp = _now_ts() + (10 * 60 * 60)
            return token_holder["tok"], approx_exp

        return None, None

    # ---------- HTTP low level ----------
    def _call_price_availability(self, payload: Dict[str, Any]) -> Tuple[int, Any]:
        """
        Realiza POST a /api/priceAvailability con headers razonables.
        """
        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": BQB_BASE,
            "referer": PRODUCT_HTML,
        }
        try:
            resp = requests.post(API_PRICE_AVAIL, json=payload, headers=headers, timeout=30)
            j = resp.json() if resp.content else {}
            return resp.status_code, j
        except Exception as e:
            return 500, str(e)

    # ---------- Payloads ----------
    def _payload_day(self, origen: str, destino: str, yy: str, mm: str, dd: str, seat_code: str = "BSEAT") -> Dict[str, Any]:
        """
        Payload base “por día completo” (1 adulto) solicitando PROGRAMADA y FLEXIBLE.
        seat_code no condiciona el bundle de salidas; lo dejamos para compatibilidad.
        """
        return {
            "token": self._get_valid_token(),
            "request": {
                "m_AGT_AgencyIdentity": {
                    "m_AGAC_AgentsAccountNumber": {"m_AGAC_AgentAccountNumber": "7250"},
                    "c_CURR_CurrencyForTransaction": {"m_6345_CurrencyCoded": "ARS", "c_U428_DecimalPrecision": "2"},
                    "c_CMPNY_Company": {
                        "m_C045_TradingUnitName": "PAX",
                        "m_C046_CompanyName": "BUQUEBUS",
                        "m_C047_GeographycalLocationName": "South America",
                        "m_C048_DivisionName": "BQB",
                    },
                    "c_SCHN_SalesChannel": {"m_C056_SalesChannel": "WEB"},
                },
                "m_RDQ_RouteDateTimeRequest": [{
                    "index": "0",
                    "m_LEGJ_LegOrSectorOfJourney": {"m_LEGJ_LegOrSectorOfJourney": "1"},
                    "m_ROUT_TravelRoute": {
                        "m_U271_ServiceAgreementDeparturePort": origen,
                        "m_U272_ServiceAgreementDestinationPort": destino,
                        "c_C276_SailingCode": "",
                        "c_C257_applyReturnFare": False,
                        "c_399_SearchVesselTransfer": "false",
                        "c_400_vesselTransferTime": 0
                    },
                    "m_DPDT_DepartureDateTime": {"m_U247_StandardDepartureDate": yy+mm+dd, "m_U248_StandardDepartureTime": "0000"},
                    "c_DPDT_DepartureDateTimeTo": {"m_U247_StandardDepartureDate": yy+mm+dd, "m_U248_StandardDepartureTime": "2359"}
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
                    "m_ACPL_AccomodationPlaces": {"m_U228_QuantityOfUnits": 1, "m_U229_ModeOfOccupancy": "C"},
                    "m_ACDT_AccomodationDetails": {
                        "m_U220_ServiceAgreementDefinedAccomodationCode": seat_code,
                        "i_U221_AccomodationType": "CHAIR"
                    },
                    "m_PAXT_PassengerType": [{"m_U257_PassengerTypeCode": "ADL", "m_U258_NumberOfPassengers": 1}]
                }],
                "c_VEQ_VehicleRequest": [],
                "c_MTT_MultipleTariffTypeRequest": [{
                    "m_LEGJ_LegOrSectorOfJourney": {"m_LEGJ_LegOrSectorOfJourney": "1"},
                    "m_TARF_TariffCodeTypeDescription": [
                        {"c_U282_TariffType": "PROGRAMADA", "c_C113_PriceDetailRequested": "true"},
                        {"c_U282_TariffType": "FLEXIBLE",   "c_C113_PriceDetailRequested": "true"},
                    ]
                }]
            }
        }

    def _payload_single_sailing_by_seat(self, origen: str, destino: str, yymmdd: str, sailing_code: str, seat_code: str) -> Dict[str, Any]:
        """
        Payload para pedir los precios de UNA salida concreta (sailing_code) con el asiento/cabina seat_code.
        """
        return {
            "token": self._get_valid_token(),
            "request": {
                "m_AGT_AgencyIdentity": {
                    "m_AGAC_AgentsAccountNumber": {"m_AGAC_AgentAccountNumber": "7250"},
                    "c_CURR_CurrencyForTransaction": {"m_6345_CurrencyCoded": "ARS", "c_U428_DecimalPrecision": "2"},
                    "c_CMPNY_Company": {
                        "m_C045_TradingUnitName": "PAX",
                        "m_C046_CompanyName": "BUQUEBUS",
                        "m_C047_GeographycalLocationName": "South America",
                        "m_C048_DivisionName": "BQB",
                    },
                    "c_SCHN_SalesChannel": {"m_C056_SalesChannel": "WEB"},
                },
                "m_RDQ_RouteDateTimeRequest": [{
                    "index": "0",
                    "m_LEGJ_LegOrSectorOfJourney": {"m_LEGJ_LegOrSectorOfJourney": "1"},
                    "m_ROUT_TravelRoute": {
                        "m_U271_ServiceAgreementDeparturePort": origen,
                        "m_U272_ServiceAgreementDestinationPort": destino,
                        "c_C276_SailingCode": sailing_code,
                        "c_C257_applyReturnFare": False,
                        "c_399_SearchVesselTransfer": "false",
                        "c_400_vesselTransferTime": 0
                    },
                    "m_DPDT_DepartureDateTime": {"m_U247_StandardDepartureDate": yymmdd, "m_U248_StandardDepartureTime": "0000"},
                    "c_DPDT_DepartureDateTimeTo": {"m_U247_StandardDepartureDate": yymmdd, "m_U248_StandardDepartureTime": "2359"}
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
                    "m_ACPL_AccomodationPlaces": {"m_U228_QuantityOfUnits": 1, "m_U229_ModeOfOccupancy": "C"},
                    "m_ACDT_AccomodationDetails": {
                        "m_U220_ServiceAgreementDefinedAccomodationCode": seat_code,
                        "i_U221_AccomodationType": "CHAIR"
                    },
                    "m_PAXT_PassengerType": [{"m_U257_PassengerTypeCode": "ADL", "m_U258_NumberOfPassengers": 1}]
                }],
                "c_VEQ_VehicleRequest": [],
                "c_MTT_MultipleTariffTypeRequest": [{
                    "m_LEGJ_LegOrSectorOfJourney": {"m_LEGJ_LegOrSectorOfJourney": "1"},
                    "m_TARF_TariffCodeTypeDescription": [
                        {"c_U282_TariffType": "PROGRAMADA", "c_C113_PriceDetailRequested": "true"},
                        {"c_U282_TariffType": "FLEXIBLE",   "c_C113_PriceDetailRequested": "true"},
                    ]
                }]
            }
        }

    # ---------- API públicos ----------
    def fetch_price(self, origen: str, destino: str, fecha: str) -> Tuple[int, Any]:
        """
        Llamada base que devuelve el “bundle” con todas las salidas del día (clave 'sailingprice').
        """
        yy, mm, dd = self._normalize_date_to_yymmdd(fecha)
        payload = self._payload_day(origen, destino, yy, mm, dd, seat_code="BSEAT")
        status, data = self._call_price_availability(payload)
        if status != 200:
            log(f"fetch_price error: {data}")
        return status, data

    def fetch_day_full(self, origen: str, destino: str, fecha: str) -> Tuple[int, List[Dict[str, Any]]]:
        """
        Por cada salida, toma PROGRAMADA y FLEXIBLE (totales) y arma:
          {
            sailingCode, dep, arr, ship, available,
            turista, business, diff
          }
        """
        status, data = self.fetch_price(origen, destino, fecha)
        if status != 200:
            return status, data

        out: List[Dict[str, Any]] = []
        for s in data.get("sailingprice", []):
            r = s.get("c_RDR_RouteDateTimeResponse", {})
            travel = r.get("m_ROUT_TravelRoute", {})
            depblk = r.get("m_DPDT_DepartureDateAndTime", {})
            arrblk = r.get("c_ARDT_ArrivalDateAndTime", {})
            ship   = r.get("c_SHNM_ShipName", {})

            programada = None
            flexible   = None
            for t in s.get("c_TCT_TariffChargesTotals", []):
                typ = (
                    t.get("c_QOR_QuotationBasisResponse", {})
                     .get("m_TARF_TariffCodeTypeDescription", {})
                     .get("c_U282_TariffType")
                )
                amt = (
                    t.get("c_QLP_ChargesTotal", {})
                     .get("m_CHTO_ChargeTotals", {})
                     .get("m_U618_TotalAmount")
                )
                val = _to_money(amt)
                if typ == "PROGRAMADA":
                    programada = val
                elif typ == "FLEXIBLE":
                    flexible = val

            item = {
                "sailingCode": travel.get("c_C276_SailingCode"),
                "dep": depblk.get("m_U248_StandardDepartureTime"),
                "arr": arrblk.get("c_U239_NominalArrivalTime"),
                "ship": ship.get("m_SHNM_ShipName"),
                "available": bool(travel.get("c_C081_IsAvailable")),
                "turista": programada,
                "business": flexible,
                "diff": (flexible - programada) if (programada is not None and flexible is not None) else None,
            }
            out.append(item)
        return 200, out
    
    def fetch_day_raw(self, origen: str, destino: str, fecha: str) -> Tuple[int, Any]:
        """
        Devuelve la respuesta cruda del endpoint /api/priceAvailability
        para el origen/destino/fecha especificados, sin procesar.
        Ideal para debug y análisis del formato real de datos de Buquebus.
        """
        log(f"Solicitando datos RAW para {origen}->{destino} en {fecha}")
        try:
            status, data = self.fetch_price(origen, destino, fecha)
            if status != 200:
                return status, {"error": data}
            return status, data
        except Exception as e:
            log(f"/price/raw error: {e}")
            return 500, {"error": str(e)}

    def fetch_day_classes(self, origen: str, destino: str, fecha: str) -> Tuple[int, List[Dict[str, Any]]]:
        log(f"Solicitando clases con tarifas para {origen}->{destino} en {fecha}")
        status, data = self.fetch_price(origen, destino, fecha)
        if status != 200:
            return status, data

        out: List[Dict[str, Any]] = []

        for s in data.get("sailingprice", []):
            r = s.get("c_RDR_RouteDateTimeResponse", {})
            travel = r.get("m_ROUT_TravelRoute", {})
            depblk = r.get("m_DPDT_DepartureDateAndTime", {})
            arrblk = r.get("c_ARDT_ArrivalDateAndTime", {})
            ship = r.get("c_SHNM_ShipName", {})

            tarifas = []

            for pp in s.get("productPrice", []):
                tipo_tarifa = pp.get("c_U282_TariffType")

                # --- Precio base (turista) ---
                base_total = 0
                par = pp.get("c_PAR_PassengerDetailsResponse", [])
                if par:
                    paxset = par[0].get("m_PAXS_PassengerSet", [])
                    if paxset:
                        base_total += _sum_price_details(paxset[0].get("c_PRIC_PriceDetails"))

                # --- Precio adicional (business) ---
                business_total = base_total
                acr = pp.get("c_ACR_AccomodationDetailsResponse", [])
                if acr:
                    for acc in acr:
                        details = acc.get("m_ACPL_AccomodationPlaces", {})
                        business_total += _sum_price_details(details.get("c_PRIC_PriceDetails"))

                tarifas.append({
                    "tipoTarifa": tipo_tarifa,
                    "turista": base_total,
                    "business": business_total,
                    "diff": (business_total - base_total) if business_total and base_total else None
                })

            out.append({
                "sailingCode": travel.get("c_C276_SailingCode"),
                "dep": depblk.get("m_U248_StandardDepartureTime"),
                "arr": arrblk.get("c_U239_NominalArrivalTime"),
                "ship": ship.get("m_SHNM_ShipName"),
                "available": bool(travel.get("c_C081_IsAvailable")),
                "tarifas": tarifas
            })

        return 200, out
    
    def fetch_day_full_preciso(self, origen: str, destino: str, fecha: str) -> Tuple[int, List[Dict[str, Any]]]:
        """
        Combina los valores de pasajeros y acomodaciones (Turista/Business)
        para devolver precios exactos iguales a los que muestra la web.
        """
        status, data = self.fetch_price(origen, destino, fecha)
        if status != 200:
            return status, data

        out: List[Dict[str, Any]] = []

        for s in data.get("sailingprice", []):
            r = s.get("c_RDR_RouteDateTimeResponse", {})
            travel = r.get("m_ROUT_TravelRoute", {})
            depblk = r.get("m_DPDT_DepartureDateAndTime", {})
            arrblk = r.get("c_ARDT_ArrivalDateAndTime", {})
            ship = r.get("c_SHNM_ShipName", {})

            tarifas = []

            for t in s.get("productPrice", []):
                tipo_tarifa = t.get("c_U282_TariffType")

                # Buscar precios de pasajero (turista)
                turista_total = 0.0
                for par in t.get("c_PAR_PassengerDetailsResponse", []):
                    for ps in par.get("m_PAXS_PassengerSet", []):
                        for pd in ps.get("c_PRIC_PriceDetails", []):
                            val = pd.get("m_C088_UnitGrossAmount")
                            if val:
                                turista_total += _to_money(val)

                # Buscar precios de acomodación (business)
                business_total = 0.0
                for acr in t.get("c_ACR_AccomodationDetailsResponse", []):
                    acpl = acr.get("m_ACPL_AccomodationPlaces", {})
                    val = acpl.get("c_U231_ChargePerUnit")
                    if val:
                        business_total += _to_money(val)

                    # Sumar surcharges dentro de acomodación
                    for pd in acpl.get("c_PRIC_PriceDetails", []):
                        val = pd.get("m_C088_UnitGrossAmount")
                        if val:
                            business_total += _to_money(val)

                diff = (business_total - turista_total) if (turista_total and business_total) else None

                tarifas.append({
                    "tipoTarifa": tipo_tarifa,
                    "turista": round(turista_total / 100, 2),
                    "business": round(business_total / 100, 2),
                    "diff": round(diff / 100, 2) if diff else None
                })

            out.append({
                "sailingCode": travel.get("c_C276_SailingCode"),
                "dep": depblk.get("m_U248_StandardDepartureTime"),
                "arr": arrblk.get("c_U239_NominalArrivalTime"),
                "ship": ship.get("m_SHNM_ShipName"),
                "available": bool(travel.get("c_C081_IsAvailable")),
                "tarifas": tarifas
            })

        return 200, out


    def fetch_day_by_seat(self, origen: str, destino: str, fecha: str, seat_code: str) -> Tuple[int, List[Dict[str, Any]]]:
        """
        Por cada salida del día, consulta el detalle de precios para el asiento/cabina seat_code.
        Devuelve ADL y los totales PROGRAMADA/FLEXIBLE (si aplica).
        """
        status, base = self.fetch_price(origen, destino, fecha)
        if status != 200:
            return status, base

        yy, mm, dd = self._normalize_date_to_yymmdd(fecha)
        yymmdd = yy + mm + dd
        items: List[Dict[str, Any]] = []

        for s in base.get("sailingprice", []):
            r = s.get("c_RDR_RouteDateTimeResponse", {})
            travel = r.get("m_ROUT_TravelRoute", {})
            depblk = r.get("m_DPDT_DepartureDateAndTime", {})
            arrblk = r.get("c_ARDT_ArrivalDateAndTime", {})
            ship   = r.get("c_SHNM_ShipName", {})

            code = travel.get("c_C276_SailingCode")
            if not code:
                continue

            payload = self._payload_single_sailing_by_seat(origen, destino, yymmdd, code, seat_code)
            st2, resp = self._call_price_availability(payload)

            adl = None
            prog_total = None
            flex_total = None

            if st2 == 200 and isinstance(resp, dict):
                # Totales
                for t in resp.get("c_TCT_TariffChargesTotals", []):
                    typ = (
                        t.get("c_QOR_QuotationBasisResponse", {})
                         .get("m_TARF_TariffCodeTypeDescription", {})
                         .get("c_U282_TariffType")
                    )
                    amt = (
                        t.get("c_QLP_ChargesTotal", {})
                         .get("m_CHTO_ChargeTotals", {})
                         .get("m_U618_TotalAmount")
                    )
                    val = _to_money(amt)
                    if typ == "PROGRAMADA":
                        prog_total = val
                    elif typ == "FLEXIBLE":
                        flex_total = val

                # ADL / pasajero
                for pp in resp.get("productPrice", []):
                    try:
                        par = pp.get("c_PAR_PassengerDetailsResponse", [])[0]
                        pax = par.get("m_PAXS_PassengerSet", [])[0]
                        adl_amt = pax.get("c_U260_ChargePerPerson")
                        adl_val = _to_money(adl_amt)
                        if adl_val is not None:
                            adl = adl_val
                            break
                    except Exception:
                        pass

            items.append({
                "sailingCode": code,
                "dep": depblk.get("m_U248_StandardDepartureTime"),
                "arr": arrblk.get("c_U239_NominalArrivalTime"),
                "ship": ship.get("m_SHNM_ShipName"),
                "available": bool(travel.get("c_C081_IsAvailable")),
                "adlGross": adl,
                "totalProgramada": prog_total,
                "totalFlexible": flex_total
            })

        return 200, items

    def get_web_prices(self, origen: str, destino: str, fecha: str):
        """
        Devuelve los precios listos para UI como en la web:
        - Turista = PROGRAMADA con asiento TSEAT (fallback ESEAT)
        - Business = PROGRAMADA con asiento BSEAT
        Formatea horarios HH:MM y calcula diferencia.
        """
        # 1) Totales por asiento
        st_bus, bus_items = self.fetch_day_by_seat(origen, destino, fecha, "BSEAT")
        if st_bus != 200:
            raise RuntimeError(str(bus_items))

        # Turista puede ser TSEAT o ESEAT según disponibilidad
        st_tur, tur_items = self.fetch_day_by_seat(origen, destino, fecha, "TSEAT")
        if st_tur != 200 or not tur_items or all(i.get("totalProgramada") is None for i in tur_items):
            st_tur2, tur_items2 = self.fetch_day_by_seat(origen, destino, fecha, "ESEAT")
            if st_tur2 == 200:
                tur_items = tur_items2

        # 2) Indexar por sailingCode para cruzar fácil
        idx_bus = {i["sailingCode"]: i for i in bus_items}
        idx_tur = {i["sailingCode"]: i for i in tur_items} if tur_items else {}

        out = []
        for code, b in idx_bus.items():
            # Business: PROGRAMADA (BSEAT)
            business = b.get("totalProgramada")

            # Turista: PROGRAMADA (TSEAT/ESEAT)
            t = idx_tur.get(code, {})
            turista = t.get("totalProgramada")

            # Si por algún motivo no viene turista, lo dejamos None (o podrías ocultar la salida)
            dep = b.get("dep")
            arr = b.get("arr")
            def hhmm(x): 
                return f"{x[:2]}:{x[2:]}" if isinstance(x, str) and len(x) == 4 else x

            diff = (business - turista) if (isinstance(business, (int, float)) and isinstance(turista, (int, float))) else None

            # Formato ARS (coma decimal, punto de miles)
            def fmt_ars(v):
                if v is None:
                    return None
                s = f"{v:,.2f}"
                return "ARS " + s.replace(",", "X").replace(".", ",").replace("X", ".")

            out.append({
                "hora_salida": hhmm(dep),
                "hora_llegada": hhmm(arr),
                "barco": b.get("ship"),
                "codigo": code,
                "turista": fmt_ars(turista),
                "business": fmt_ars(business),
                "diferencia": fmt_ars(diff),
            })

        # Ordená por hora de salida por si el server devolvió desordenado
        out.sort(key=lambda x: (x["hora_salida"] or "99:99"))
        return out

    def _post_day_pricing(self, origen: str, destino: str, fecha: str, accomodation_code: str):
        """
        Llama al endpoint de pricing para TODO el día, fijando la acomodación:
        - 'TSEAT' para Turista
        - 'BSEAT' para Business
        """
        # La API espera YYMMDD (igual que _payload_day)
        yy, mm, dd = self._normalize_date_to_yymmdd(fecha)
        yymmdd = yy + mm + dd

        payload = {
            # FALTABA el token → sin esto responde error
            "token": self._get_valid_token(),
            "request": {
                "m_AGT_AgencyIdentity": {
                    "m_AGAC_AgentsAccountNumber": {"m_AGAC_AgentAccountNumber": "7250"},
                    "c_CURR_CurrencyForTransaction": {"m_6345_CurrencyCoded": "ARS", "c_U428_DecimalPrecision": "2"},
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
                        "m_U272_ServiceAgreementDestinationPort": destino,
                        "c_C276_SailingCode": "",
                        "c_C257_applyReturnFare": False,
                        "c_399_SearchVesselTransfer": "false",
                        "c_400_vesselTransferTime": 0
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
                    "m_ACPL_AccomodationPlaces": {"m_U228_QuantityOfUnits": 1, "m_U229_ModeOfOccupancy": "C"},
                    "m_ACDT_AccomodationDetails": {
                        "m_U220_ServiceAgreementDefinedAccomodationCode": accomodation_code,
                        "i_U221_AccomodationType": "CHAIR"
                    },
                    "m_PAXT_PassengerType": [{"m_U257_PassengerTypeCode": "ADL", "m_U258_NumberOfPassengers": 1}]
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
        return self._call_price_availability(payload)


    def _totales_programada_por_sailing(self, raw_json: dict) -> dict:
        """Extrae los totales PROGRAMADA por sailing."""
        data = raw_json.get("data", raw_json)
        sailings = data.get("sailingprice", [])
        out = {}
        for s in sailings:
            r = s.get("c_RDR_RouteDateTimeResponse", {})
            travel = r.get("m_ROUT_TravelRoute", {})
            dep = r.get("m_DPDT_DepartureDateAndTime", {})
            arr = r.get("c_ARDT_ArrivalDateAndTime", {})
            ship = r.get("c_SHNM_ShipName", {})
            code = travel.get("c_C276_SailingCode")

            total_prog = None
            for t in s.get("c_TCT_TariffChargesTotals", []):
                tipo = (
                    t.get("c_QOR_QuotationBasisResponse", {})
                    .get("m_TARF_TariffCodeTypeDescription", {})
                    .get("c_U282_TariffType")
                )
                if tipo == "PROGRAMADA":
                    total_str = (
                        t.get("c_QLP_ChargesTotal", {})
                        .get("m_CHTO_ChargeTotals", {})
                        .get("m_U618_TotalAmount")
                    )
                    if total_str and str(total_str).upper() != "N/A":
                        try:
                            total_prog = float(total_str) / 100
                            break
                        except ValueError:
                            total_prog = None

            if code and total_prog is not None:
                hs = dep.get("m_U248_StandardDepartureTime", "")
                ha = arr.get("c_U239_NominalArrivalTime", "")
                out[code] = (
                    total_prog,
                    (hs[:2] + ":" + hs[2:]) if len(hs) == 4 else "",
                    (ha[:2] + ":" + ha[2:]) if len(ha) == 4 else "",
                    ship.get("m_SHNM_ShipName")
                )
        return out

    def fetch_day_web_prices(self, origen: str, destino: str, fecha: str):
        """
        Devuelve precios exactos como en la web:
        - Turista: PROGRAMADA (TSEAT)
        - Business: PROGRAMADA (BSEAT)
        """
        status_t, raw_t = self._post_day_pricing(origen, destino, fecha, "TSEAT")
        if status_t != 200:
            return status_t, raw_t
        mapa_t = self._totales_programada_por_sailing(raw_t)

        status_b, raw_b = self._post_day_pricing(origen, destino, fecha, "BSEAT")
        if status_b != 200:
            return status_b, raw_b
        mapa_b = self._totales_programada_por_sailing(raw_b)

        def fmt(v): return f"ARS {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        results = []
        for code in sorted(set(mapa_t.keys()) | set(mapa_b.keys())):
            tur, hs, ha, ship = mapa_t.get(code, (None, "", "", None))
            bus, _, _, ship_b = mapa_b.get(code, (None, "", "", None))
            barco = ship or ship_b
            diff = (bus - tur) if (bus and tur) else None
            results.append({
                "hora_salida": hs,
                "hora_llegada": ha,
                "barco": barco,
                "codigo": code,
                "turista": fmt(tur) if tur else None,
                "business": fmt(bus) if bus else None,
                "diferencia": fmt(diff) if diff else None
            })

        return 200, results
    
    def _post_day_pricing_vehicle(self, origen: str, destino: str, fecha: str, accomodation_code: str):
        """
        Igual que _post_day_pricing pero agregando 1 vehículo tipo CAR.
        """
        yy, mm, dd = self._normalize_date_to_yymmdd(fecha)
        yymmdd = yy + mm + dd

        payload = {
            "token": self._get_valid_token(),
            "request": {
                "m_AGT_AgencyIdentity": {
                    "m_AGAC_AgentsAccountNumber": {"m_AGAC_AgentAccountNumber": "7250"},
                    "c_CURR_CurrencyForTransaction": {"m_6345_CurrencyCoded": "ARS", "c_U428_DecimalPrecision": "2"},
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
                        "m_U272_ServiceAgreementDestinationPort": destino,
                        "c_C276_SailingCode": "",
                        "c_C257_applyReturnFare": False,
                        "c_399_SearchVesselTransfer": "false",
                        "c_400_vesselTransferTime": 0
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
                    "m_ACPL_AccomodationPlaces": {"m_U228_QuantityOfUnits": 1, "m_U229_ModeOfOccupancy": "C"},
                    "m_ACDT_AccomodationDetails": {
                        "m_U220_ServiceAgreementDefinedAccomodationCode": accomodation_code,
                        "i_U221_AccomodationType": "CHAIR"
                    },
                    "m_PAXT_PassengerType": [{"m_U257_PassengerTypeCode": "ADL", "m_U258_NumberOfPassengers": 1}]
                }],

                # 🔥 AGREGAR AUTO
                "c_VEQ_VehicleRequest": [{
                    "index": "0",
                    "m_LEGJ_LegOrSectorOfJourney": {"m_LEGJ_LegOrSectorOfJourney": "1"},
                    "m_VEQS_VehicleSet": [{
                        "vehicleIndex": "0",
                        "m_U259_VehicleTypeCode": "CAR",
                        "m_U260_NumberOfVehicles": 1
                    }]
                }],

                "c_MTT_MultipleTariffTypeRequest": [{
                    "m_LEGJ_LegOrSectorOfJourney": {"m_LEGJ_LegOrSectorOfJourney": "1"},
                    "m_TARF_TariffCodeTypeDescription": [
                        {"c_U282_TariffType": "PROGRAMADA", "c_C113_PriceDetailRequested": "true"}
                    ]
                }]
            }
        }
        return self._call_price_availability(payload)
    
    def fetch_day_web_prices_with_vehicle(self, origen: str, destino: str, fecha: str):
        status_t, raw_t = self._post_day_pricing_vehicle(origen, destino, fecha, "TSEAT")
        if status_t != 200:
            return status_t, raw_t
        mapa_t = self._totales_programada_por_sailing(raw_t)

        status_b, raw_b = self._post_day_pricing_vehicle(origen, destino, fecha, "BSEAT")
        if status_b != 200:
            return status_b, raw_b
        mapa_b = self._totales_programada_por_sailing(raw_b)

        def fmt(v): 
            return f"ARS {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

        results = []
        for code in sorted(set(mapa_t.keys()) | set(mapa_b.keys())):
            tur, hs, ha, ship = mapa_t.get(code, (None, "", "", None))
            bus, _, _, ship_b = mapa_b.get(code, (None, "", "", None))
            barco = ship or ship_b
            diff = (bus - tur) if (bus and tur) else None
            results.append({
                "hora_salida": hs,
                "hora_llegada": ha,
                "barco": barco,
                "codigo": code,
                "turista": fmt(tur) if tur else None,
                "business": fmt(bus) if bus else None,
                "vehiculo": True,
                "diferencia": fmt(diff) if diff else None
            })

        return 200, results


