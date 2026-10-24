# main.py
# FastAPI backend for Suaru: plan endpoints + serving static/index.html.

import asyncio
import pathlib
import time

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import calc

BASE_DIR = pathlib.Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Small in-memory cache: the same field is often submitted several times.
WEATHER_CACHE = {}
CACHE_TTL_SECONDS = 600
MAX_FIELDS = 20

app = FastAPI(title="Suaru", description="Оценка воды, дизеля и CO2 для поля")


# ── Models ───────────────────────────────────────────────────
class PlanRequest(BaseModel):
    lat: float = Field(..., description="Широта", examples=[43.24])
    lon: float = Field(..., description="Долгота", examples=[76.89])
    crop: str = Field(
        ...,
        description="Культура: wheat, barley, corn, potato, rice",
        examples=["wheat"],
    )
    stage: str = Field(
        "mid",
        description="Стадия роста: ini (начало), mid (середина), end (конец)",
        examples=["mid"],
    )
    area_ha: float = Field(..., description="Площадь поля, га", examples=[10])
    l_per_h: float = Field(..., description="Расход топлива, л/ч", examples=[12])
    ha_per_h: float = Field(..., description="Скорость обработки, га/ч", examples=[2])
    days: int = Field(3, description="Сколько дней считать (1–7)", examples=[3])


class DailyPlan(BaseModel):
    date: str = Field(..., description="Дата, YYYY-MM-DD", examples=["2026-10-24"])
    et0_mm: float = Field(..., description="ET0 за день, мм", examples=[1.6])
    rain_mm: float = Field(..., description="Осадки за день, мм", examples=[0.0])
    need_mm: float = Field(..., description="Потребность в воде за день, мм", examples=[1.84])
    need_m3: float = Field(..., description="Объём воды за день, м³", examples=[184.0])


class PlanResponse(BaseModel):
    mm: float = Field(..., description="Суммарная потребность в воде, мм", examples=[4.83])
    m3: float = Field(..., description="Суммарный объём воды, м³", examples=[483.0])
    diesel_l: float = Field(..., description="Дизель на обработку, л", examples=[60.0])
    co2_kg: float = Field(..., description="CO2 от дизеля, кг", examples=[159.0])
    stage: str = Field(..., description="Стадия роста", examples=["mid"])
    kc: float = Field(..., description="Коэффициент культуры Kc для выбранной стадии", examples=[1.15])
    daily: list[DailyPlan] = Field(..., description="Разбивка по дням")


class BatchField(PlanRequest):
    """Одно поле из списка: те же поля, что в /api/plan, плюс название."""

    name: str = Field("", description="Название поля (необязательно)", examples=["Поле у реки"])


class FieldPlan(BaseModel):
    name: str = Field(..., description="Название поля", examples=["Поле у реки"])
    plan: PlanResponse


class BatchTotals(BaseModel):
    area_ha: float = Field(..., description="Суммарная площадь, га", examples=[20])
    m3: float = Field(..., description="Суммарный объём воды, м³", examples=[989.0])
    diesel_l: float = Field(..., description="Суммарный дизель, л", examples=[120.0])
    co2_kg: float = Field(..., description="Суммарный CO2 от дизеля, кг", examples=[318.0])


class BatchRequest(BaseModel):
    fields: list[BatchField] = Field(
        ...,
        description=f"Полей: от 1 до {MAX_FIELDS}",
        examples=[
            [
                {
                    "name": "Поле у реки",
                    "lat": 43.24,
                    "lon": 76.89,
                    "crop": "wheat",
                    "stage": "mid",
                    "area_ha": 10,
                    "l_per_h": 12,
                    "ha_per_h": 2,
                    "days": 3,
                }
            ]
        ],
    )


class BatchResponse(BaseModel):
    fields: list[FieldPlan] = Field(..., description="План по каждому полю")
    totals: BatchTotals = Field(..., description="Суммы по всем полям")


# Pydantic errors (e.g. text instead of a number) → 400 in Russian
@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc):
    return JSONResponse(status_code=400, content={"detail": "Проверьте данные формы"})


# ── Open-Meteo ───────────────────────────────────────────────
def cache_key(lat: float, lon: float, days: int):
    """Round coordinates so nearby clicks share one weather request."""
    return (round(lat, 2), round(lon, 2), days)


async def fetch_weather(lat: float, lon: float, days: int):
    """Return (dates, et0 list, precipitation list) for the next `days` days."""
    key = cache_key(lat, lon, days)
    now = time.monotonic()
    cached = WEATHER_CACHE.get(key)
    if cached and cached[0] > now:
        return cached[1]

    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "et0_fao_evapotranspiration,precipitation_sum",
        "forecast_days": days,
        "timezone": "auto",
    }

    data = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(OPEN_METEO_URL, params=params)
                resp.raise_for_status()
            try:
                data = resp.json()
            except ValueError:
                raise HTTPException(status_code=502, detail="Open-Meteo вернул некорректный ответ")
            break
        except httpx.HTTPError:
            if attempt == 0:
                await asyncio.sleep(0.5)  # one quick retry, then give up
                continue
            raise HTTPException(status_code=502, detail="Сервис погоды Open-Meteo не отвечает")

    daily = data.get("daily") if isinstance(data, dict) else None
    if not isinstance(daily, dict):
        raise HTTPException(status_code=502, detail="Open-Meteo вернул неполные данные")

    dates = daily.get("time") or []
    et0 = daily.get("et0_fao_evapotranspiration") or []
    rain = daily.get("precipitation_sum") or []
    if not isinstance(dates, list) or not isinstance(et0, list) or not isinstance(rain, list):
        raise HTTPException(status_code=502, detail="Open-Meteo вернул неполные данные")
    if len(dates) < days or len(et0) < days or len(rain) < days:
        raise HTTPException(status_code=502, detail="Open-Meteo вернул неполные данные")

    dates, et0, rain = dates[:days], et0[:days], rain[:days]
    if any(not isinstance(value, (int, float)) for value in et0 + rain):
        raise HTTPException(status_code=502, detail="Open-Meteo вернул неполные данные")

    result = (dates, et0, rain)
    WEATHER_CACHE[key] = (now + CACHE_TTL_SECONDS, result)
    return result


# ── Helpers for one field ──────────────────────────────────
def validate_field(req: PlanRequest, prefix: str = ""):
    """Check one field and raise 400 with a Russian message.

    `prefix` names the field inside a batch, e.g. "Поле 2: ".
    """
    if req.crop not in calc.KC:
        raise HTTPException(status_code=400, detail=f"{prefix}Неизвестная культура")
    if req.stage not in calc.STAGES:
        raise HTTPException(status_code=400, detail=f"{prefix}Неизвестная стадия роста")
    if req.area_ha <= 0:
        raise HTTPException(status_code=400, detail=f"{prefix}Площадь поля должна быть больше нуля")
    if req.l_per_h <= 0:
        raise HTTPException(status_code=400, detail=f"{prefix}Расход топлива должен быть больше нуля")
    if req.ha_per_h <= 0:
        raise HTTPException(status_code=400, detail=f"{prefix}Скорость обработки должна быть больше нуля")
    if not (-90 <= req.lat <= 90 and -180 <= req.lon <= 180):
        raise HTTPException(status_code=400, detail=f"{prefix}Координаты вне допустимого диапазона")
    if not (1 <= req.days <= 7):
        raise HTTPException(status_code=400, detail=f"{prefix}Количество дней должно быть от 1 до 7")


async def plan_for(req: PlanRequest, weather=None):
    """Fetch weather and calculate the plan for one validated field.

    `weather` lets a batch pass an already fetched (and cached) result.
    """
    kc = calc.crop_kc(req.crop, req.stage)
    if weather is None:
        weather = await fetch_weather(req.lat, req.lon, req.days)
    dates, et0, rain = weather

    daily = []
    total_mm = 0.0
    for i in range(req.days):
        need = calc.daily_water_mm(et0[i], rain[i], kc)
        total_mm += need
        daily.append(
            {
                "date": dates[i],
                "et0_mm": round(et0[i], 2),
                "rain_mm": round(rain[i], 2),
                "need_mm": round(need, 2),
                "need_m3": round(calc.water_volume_m3(need, req.area_ha), 2),
            }
        )

    m3 = calc.water_volume_m3(total_mm, req.area_ha)
    diesel = calc.diesel_liters(req.area_ha, req.l_per_h, req.ha_per_h)
    co2 = calc.co2_from_diesel(diesel)

    return {
        "mm": round(total_mm, 2),
        "m3": round(m3, 2),
        "diesel_l": round(diesel, 2),
        "co2_kg": round(co2, 2),
        "stage": req.stage,
        "kc": kc,
        "daily": daily,
    }


# ── POST /api/plan ───────────────────────────────────────────
@app.post(
    "/api/plan",
    response_model=PlanResponse,
    responses={
        400: {"description": "Неверные данные формы"},
        502: {"description": "Open-Meteo не ответил"},
    },
)
async def api_plan(req: PlanRequest):
    validate_field(req)
    return await plan_for(req)


# ── POST /api/plan/batch («Мои поля») ─────────────────────────
@app.post(
    "/api/plan/batch",
    response_model=BatchResponse,
    responses={
        400: {"description": "Неверные данные полей"},
        502: {"description": "Open-Meteo не ответил"},
    },
)
async def api_plan_batch(req: BatchRequest):
    if not req.fields:
        raise HTTPException(status_code=400, detail="Добавьте хотя бы одно поле")
    if len(req.fields) > MAX_FIELDS:
        raise HTTPException(status_code=400, detail=f"Слишком много полей (максимум {MAX_FIELDS})")

    # Validate every field before calling the weather API.
    labels = []
    for index, field in enumerate(req.fields, start=1):
        name = field.name.strip()
        labels.append(name or f"Поле {index}")
        prefix = f"Поле «{name}» ({index}): " if name else f"Поле {index}: "
        validate_field(field, prefix)

    # The same coordinates share one weather request: fetch each place once,
    # then calculate every field from its own Kc.
    places = {}
    for field in req.fields:
        places.setdefault(cache_key(field.lat, field.lon, field.days), field)
    weather = await asyncio.gather(
        *(fetch_weather(f.lat, f.lon, f.days) for f in places.values())
    )
    by_place = dict(zip(places.keys(), weather))

    plans = await asyncio.gather(
        *(
            plan_for(field, by_place[cache_key(field.lat, field.lon, field.days)])
            for field in req.fields
        )
    )

    fields = []
    totals = {"area_ha": 0.0, "m3": 0.0, "diesel_l": 0.0, "co2_kg": 0.0}
    for field, name, plan in zip(req.fields, labels, plans):
        fields.append({"name": name, "plan": plan})
        totals["area_ha"] += field.area_ha
        totals["m3"] += plan["m3"]
        totals["diesel_l"] += plan["diesel_l"]
        totals["co2_kg"] += plan["co2_kg"]

    totals = {key: round(value, 2) for key, value in totals.items()}
    return {"fields": fields, "totals": totals}


# ── Static frontend (mount LAST so /api stays on top) ────────
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
