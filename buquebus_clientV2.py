# -*- coding: utf-8 -*-
from typing import Any, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor
import requests

from buquebus_client import BuquebusClient


class BuquebusClientV2(BuquebusClient):
    """
    V2:
    - Mantiene compatibilidad total con V1
    - Precios WEB PRO
    - llamadas concurrentes (igual que la web real)
    """

    def __init__(self, headless: bool = True, timeout_ms: int = 30000):
        super().__init__(headless=headless, timeout_ms=timeout_ms)

        # keep-alive → mejora performance
        self._session = requests.Session()

    # --------------------------------------------------
    # override para usar Session (mas rápido)
    # --------------------------------------------------
    def _call_price_availability(self, payload: Dict[str, Any]) -> Tuple[int, Any]:
        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://www.buquebus.com",
            "referer": "https://www.buquebus.com/ar/product",
        }
        try:
            resp = self._session.post(
                "https://www.buquebus.com/api/priceAvailability",
                json=payload,
                headers=headers,
                timeout=30
            )
            j = resp.json() if resp.content else {}
            return resp.status_code, j
        except Exception as e:
            return 500, str(e)

    # --------------------------------------------------
    # ECONOMICA (tarifa separada)
    # --------------------------------------------------
    def _post_day_pricing_economica(self, origen, destino, fecha, accomodation_code="ESEAT"):
        yy, mm, dd = self._normalize_date_to_yymmdd(fecha)

        payload = self._payload_day(origen, destino, yy, mm, dd, accomodation_code)

        payload["request"]["c_MTT_MultipleTariffTypeRequest"][0][
            "m_TARF_TariffCodeTypeDescription"
        ] = [
            {"c_U282_TariffType": "ECONOMICA", "c_C113_PriceDetailRequested": "true"}
        ]

        return self._call_price_availability(payload)

    # --------------------------------------------------
    # extractor genérico por tarifa
    # --------------------------------------------------
    def _totales_por_tarifa(self, raw_json, tarifa: str) -> dict:

        if not isinstance(raw_json, dict):
            return {}

        payload = raw_json.get("data") if isinstance(raw_json.get("data"), dict) else raw_json

        out = {}

        for s in payload.get("sailingprice", []):
            r = s.get("c_RDR_RouteDateTimeResponse", {})
            travel = r.get("m_ROUT_TravelRoute", {})
            dep = r.get("m_DPDT_DepartureDateAndTime", {})
            arr = r.get("c_ARDT_ArrivalDateAndTime", {})
            ship = r.get("c_SHNM_ShipName", {})

            code = travel.get("c_C276_SailingCode")
            total = None

            for t in s.get("c_TCT_TariffChargesTotals", []):
                tipo = (
                    t.get("c_QOR_QuotationBasisResponse", {})
                     .get("m_TARF_TariffCodeTypeDescription", {})
                     .get("c_U282_TariffType")
                )

                if tipo == tarifa:
                    amt = (
                        t.get("c_QLP_ChargesTotal", {})
                         .get("m_CHTO_ChargeTotals", {})
                         .get("m_U618_TotalAmount")
                    )
                    if amt and str(amt).upper() != "N/A":
                        try:
                            total = float(amt) / 100
                        except Exception:
                            total = None
                    break

            if code and total is not None:
                hs = dep.get("m_U248_StandardDepartureTime", "")
                ha = arr.get("c_U239_NominalArrivalTime", "")

                out[code] = (
                    total,
                    hs[:2] + ":" + hs[2:] if len(hs) == 4 else None,
                    ha[:2] + ":" + ha[2:] if len(ha) == 4 else None,
                    ship.get("m_SHNM_ShipName"),
                )

        return out

    # --------------------------------------------------
    # PRO METHOD (ESTABLE + EXACTO)
    # --------------------------------------------------
    def fetch_day_web_prices_pro(self, origen, destino, fecha):

        # 🔥 asegurar token antes del paralelo
        self._get_valid_token()

        # EXACTAMENTE igual que la web real → en paralelo
        with ThreadPoolExecutor(max_workers=4) as ex:
            fut_t = ex.submit(self._post_day_pricing, origen, destino, fecha, "TSEAT")
            fut_b = ex.submit(self._post_day_pricing, origen, destino, fecha, "BSEAT")
            fut_p = ex.submit(self._post_day_pricing, origen, destino, fecha, "PRSEAT")
            fut_e = ex.submit(self._post_day_pricing_economica, origen, destino, fecha, "ESEAT")

            st_t, raw_t = fut_t.result()
            st_b, raw_b = fut_b.result()
            st_p, raw_p = fut_p.result()
            st_e, raw_e = fut_e.result()

        if st_t != 200:
            return st_t, raw_t
        if st_b != 200:
            return st_b, raw_b
        if st_p != 200:
            return st_p, raw_p

        mapa_t = self._totales_programada_por_sailing(raw_t)
        mapa_b = self._totales_programada_por_sailing(raw_b)
        mapa_p = self._totales_programada_por_sailing(raw_p)
        mapa_e = self._totales_por_tarifa(raw_e, "ECONOMICA") if st_e == 200 else {}

        def fmt(v):
            if v is None:
                return None
            return f"ARS {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

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
