# -*- coding: utf-8 -*-
from typing import Any, List, Optional, Dict
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import os, json, base64, time
from buquebus_clientV2 import BuquebusClientV2
from buquebus_clientUYU import BuquebusClientUYU

from buquebus_client import BuquebusClient, LOG_FILE, log, CACHE_TOKEN_FILE

app = FastAPI(title="Buquebus Price API", version="1.1.0")
client = BuquebusClient(headless=True)
client_v2 = BuquebusClientV2(headless=True)
client_uyu = BuquebusClientUYU(headless=True)

# ---------- Pydantic models ----------
class PriceRequest(BaseModel):
    origen: str = Field(..., description="Código de origen, ej: BUE")
    destino: str = Field(..., description="Código de destino, ej: MVD/COL")
    fecha: str = Field(..., description="YYYY-MM-DD | DD/MM/YYYY | YYMMDD")

class PriceResponse(BaseModel):
    status: int
    data: Any

class FullItem(BaseModel):
    sailingCode: str
    dep: Optional[str] = None
    arr: Optional[str] = None
    ship: Optional[str] = None
    available: bool
    turista: Optional[float] = None
    business: Optional[float] = None
    diff: Optional[float] = None

# ---- By-seat models ----
class BySeatRequest(PriceRequest):
    seat: str = Field(..., description="Accommodation code (ESEAT/TSEAT/BSEAT/PRSEAT)")

class BySeatItem(BaseModel):
    sailingCode: str
    dep: Optional[str] = None
    arr: Optional[str] = None
    ship: Optional[str] = None
    available: bool
    adlGross: Optional[float] = None
    totalProgramada: Optional[float] = None
    totalFlexible: Optional[float] = None

class BySeatResponse(BaseModel):
    status: int
    query: Dict[str, str]
    items: List[BySeatItem]

# ---------- Endpoints ----------
@app.post("/price", response_model=PriceResponse, summary="Consulta precio/disponibilidad (bundle del día)")
def price(req: PriceRequest):
    try:
        status, data = client.fetch_price(req.origen.upper(), req.destino.upper(), req.fecha)
        return {"status": status, "data": data}
    except Exception as e:
        log(f"/price error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/price/full", response_model=List[FullItem], summary="PROGRAMADA (turista) y FLEXIBLE (business) por salida")
def price_full(req: PriceRequest):
    try:
        status, data = client.fetch_day_full(req.origen.upper(), req.destino.upper(), req.fecha)
        if status != 200:
            raise HTTPException(status_code=500, detail=data)
        return data
    except Exception as e:
        log(f"/price/full error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/price/classes", response_model=PriceResponse)
def price_classes(req: PriceRequest):
    try:
        status, data = client.fetch_day_classes(req.origen.upper(), req.destino.upper(), req.fecha)
        return {"status": status, "data": data}
    except Exception as e:
        log(f"/price/classes error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/price/by-seat", response_model=BySeatResponse, summary="Detalle por asiento/cabina para todas las salidas del día")
def price_by_seat(req: BySeatRequest):
    try:
        status, payload = client.fetch_day_by_seat(
            req.origen.upper(), req.destino.upper(), req.fecha, req.seat.upper()
        )
        if status != 200:
            raise HTTPException(status_code=500, detail=payload)
        return {
            "status": status,
            "query": {
                "origen": req.origen.upper(),
                "destino": req.destino.upper(),
                "fecha": req.fecha,
                "seat": req.seat.upper(),
            },
            "items": payload,
        }
    except Exception as e:
        log(f"/price/by-seat error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health", summary="Healthcheck")
def health():
    return {"ok": True, "log": str(LOG_FILE.resolve())}

@app.get("/debug/token")
def debug_token():
    tok = None
    if os.environ.get("BQB_STATIC_TOKEN"):
        tok = os.environ["BQB_STATIC_TOKEN"]
    elif CACHE_TOKEN_FILE.exists():
        try:
            tok = json.loads(CACHE_TOKEN_FILE.read_text(encoding="utf-8")).get("token")
        except Exception:
            pass
    if not tok:
        return {"hasToken": False}
    parts = tok.split(".")
    payload = {}
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "==="))
    except Exception:
        pass
    exp = payload.get("exp")
    valid = (exp is None) or (int(exp) > int(time.time()))
    masked = tok[:10] + "..." + tok[-10:] if len(tok) > 24 else "masked"
    return {"hasToken": True, "masked": masked, "valid": valid, "exp": exp}

@app.post("/price/raw", response_model=PriceResponse, summary="Devuelve la respuesta completa del endpoint de Buquebus")
def price_raw(req: PriceRequest):
    try:
        status, data = client.fetch_day_raw(req.origen.upper(), req.destino.upper(), req.fecha)
        return {"status": status, "data": data}
    except Exception as e:
        log(f"/price/raw error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/price/preciso", summary="Devuelve las tarifas tal como aparecen en la web (base + acomodación)")
def price_preciso(req: PriceRequest):
    try:
        status, data = client.fetch_day_classes(
            req.origen.upper(),
            req.destino.upper(),
            req.fecha
        )
        return {"status": status, "data": data}
    except Exception as e:
        log(f"/price/preciso error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/price/web", summary="Precios como se muestran en la web")
def price_web(req: PriceRequest):
    try:
        status, data = client.fetch_day_web_prices(req.origen.upper(), req.destino.upper(), req.fecha)
        return {"status": status, "data": data}
    except Exception as e:
        log(f"/price/web error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/price/web-vehiculo")
def price_web_vehiculo(req: PriceRequest):
    try:
        status, data = client.fetch_day_web_prices_with_vehicle(
            req.origen.upper(),
            req.destino.upper(),
            req.fecha
        )
        return {"status": status, "data": data}
    except Exception as e:
        log(f"/price/web-vehiculo error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/price/web-full")
def price_web_full(req: PriceRequest):
    try:
        status, data = client.fetch_day_web_prices_full(
            req.origen.upper(),
            req.destino.upper(),
            req.fecha
        )
        return {"status": status, "data": data}
    except Exception as e:
        log(f"/price/web-full error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/price/web-pro")
def price_web_pro(req: PriceRequest):
    try:
        status, data = client_v2.fetch_day_web_prices_pro(
            req.origen.upper(),
            req.destino.upper(),
            req.fecha
        )
        return {"status": status, "data": data}
    except Exception as e:
        log(f"/price/web-pro error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/price/web-pro-uyu")
def price_web_pro_uyu(req: PriceRequest):
    status, data = client_uyu.fetch_day_web_prices_pro(
        req.origen.upper(),
        req.destino.upper(),
        req.fecha
    )
    return {"status": status, "data": data}
