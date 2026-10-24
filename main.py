# main.py
# FastAPI backend for Suaru: POST /api/plan + serving static/index.html.

import asyncio
import pathlib
import time

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import auth
import calc

BASE_DIR = pathlib.Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Small in-memory cache: the same field is often submitted several times.
WEATHER_CACHE = {}
CACHE_TTL_SECONDS = 600

app = FastAPI(title="Suaru", description="Оценка воды, дизеля и CO2 для поля")

# ── Auth gate: no session cookie -> login page (HTML) or 401 (API) ──
SESSION_COOKIE = "suaru_session"
# Only the login page and the auth endpoints themselves are public;
# everything else (/, static/, /api/plan, /docs) requires a session.
PUBLIC_PATHS = {
    "/login",
    "/favicon.ico",
    "/api/auth/register",
    "/api/auth/login",
    "/api/auth/logout",
}

auth.init_db()


@app.middleware("http")
async def require_login(request, call_next):
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    if auth.get_user_id(request.cookies.get(SESSION_COOKIE, "")):
        return await call_next(request)
    # Browser navigation -> redirect to the login page; XHR/curl -> 401 JSON.
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/login", status_code=302)
    return JSONResponse(status_code=401, content={"detail": "Требуется вход"})


# ── Models ───────────────────────────────────────────────────
class PlanRequest(BaseModel):
    lat: float = Field(..., description="Широта", examples=[43.24])
    lon: float = Field(..., description="Долгота", examples=[76.89])
    crop: str = Field(
        ...,
        description="Культура: wheat, barley, corn, potato, rice",
        examples=["wheat"],
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
    kc: float = Field(..., description="Коэффициент культуры Kc (mid-season)", examples=[1.15])
    daily: list[DailyPlan] = Field(..., description="Разбивка по дням")


class AuthRequest(BaseModel):
    username: str = Field(..., description="Логин: 3–32 символа, буквы, цифры, _", examples=["fermer"])
    password: str = Field(..., description="Пароль: минимум 6 символов", examples=["pole2026"])


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
    # Validation with clear Russian messages
    if req.crop not in calc.KC:
        raise HTTPException(status_code=400, detail="Неизвестная культура")
    if req.area_ha <= 0:
        raise HTTPException(status_code=400, detail="Площадь поля должна быть больше нуля")
    if req.l_per_h <= 0:
        raise HTTPException(status_code=400, detail="Расход топлива должен быть больше нуля")
    if req.ha_per_h <= 0:
        raise HTTPException(status_code=400, detail="Скорость обработки должна быть больше нуля")
    if not (-90 <= req.lat <= 90 and -180 <= req.lon <= 180):
        raise HTTPException(status_code=400, detail="Координаты вне допустимого диапазона")
    if not (1 <= req.days <= 7):
        raise HTTPException(status_code=400, detail="Количество дней должно быть от 1 до 7")

    kc = calc.KC[req.crop]
    dates, et0, rain = await fetch_weather(req.lat, req.lon, req.days)

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
        "kc": kc,
        "daily": daily,
    }


# ── Auth: register / login / logout / current user ──────────
@app.get("/login")
async def login_page():
    return FileResponse(STATIC_DIR / "login.html")


def _session_response(payload: dict, token: str, status_code: int = 200):
    response = JSONResponse(content=payload, status_code=status_code)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=auth.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@app.post("/api/auth/register", status_code=201)
async def api_register(req: AuthRequest):
    try:
        user_id = auth.register_user(req.username, req.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    token = auth.create_session(user_id)
    return _session_response({"username": auth.get_username(user_id)}, token, status_code=201)


@app.post("/api/auth/login")
async def api_login(req: AuthRequest):
    user_id = auth.verify_user(req.username, req.password)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    token = auth.create_session(user_id)
    return _session_response({"username": auth.get_username(user_id)}, token)


@app.post("/api/auth/logout")
async def api_logout(request: Request):
    auth.delete_session(request.cookies.get(SESSION_COOKIE, ""))
    response = JSONResponse(content={"detail": "Вы вышли"})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/auth/me")
async def api_me(request: Request):
    user_id = auth.get_user_id(request.cookies.get(SESSION_COOKIE, ""))
    if user_id is None:  # middleware already blocks this; kept for clarity
        raise HTTPException(status_code=401, detail="Требуется вход")
    return {"username": auth.get_username(user_id)}


# ── Static frontend (mount LAST so /api stays on top) ────────
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
