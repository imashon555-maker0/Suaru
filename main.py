# main.py
# FastAPI backend for Suaru: POST /api/plan + serving static/index.html.

import asyncio
import os
import pathlib
import time
import urllib.parse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import auth
import calc
import mailer

BASE_DIR = pathlib.Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Small in-memory cache: the same field is often submitted several times.
WEATHER_CACHE = {}
CACHE_TTL_SECONDS = 600

app = FastAPI(title="Suaru", description="Water, diesel and CO2 estimate for a field")

# ── Session cookie, auth gate and request hardening ──────────
SESSION_COOKIE = "suaru_session"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Only the sign-in page, the health check and the auth endpoints are public;
# everything else (/, static/, /api/plan, /docs) requires a session.
PUBLIC_PATHS = {
    "/login",
    "/favicon.ico",
    "/verify",
    "/api/health",
    "/api/auth/register",
    "/api/auth/login",
    "/api/auth/logout",
}

# In-memory rate limits: (bucket, key) -> attempt timestamps.
# A single Render instance means one shared process is enough for a demo.
RATE_LIMITS = {}


def rate_limited(bucket: str, key: str, limit: int, window_seconds: int) -> bool:
    """True when the key has spent its allowance; records the attempt otherwise."""
    now = time.monotonic()
    cache_key = (bucket, key)
    attempts = [t for t in RATE_LIMITS.get(cache_key, []) if now - t < window_seconds]
    if len(attempts) >= limit:
        RATE_LIMITS[cache_key] = attempts
        return True
    attempts.append(now)
    RATE_LIMITS[cache_key] = attempts
    return False


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # Leaflet and its marker icons come from unpkg, map tiles from OSM.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "img-src 'self' data: https://*.tile.openstreetmap.org https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
}


def secure_response(response, request):
    response.headers.update(SECURITY_HEADERS)
    if request.headers.get("x-forwarded-proto", "").lower() == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


auth.init_db()


@app.middleware("http")
async def request_guard(request, call_next):
    path = request.url.path

    # CSRF: refuse cross-site state changes. Requests without Origin (curl,
    # server-to-server) are allowed; browsers always send it on POST.
    origin = request.headers.get("origin")
    if origin and request.method in UNSAFE_METHODS:
        if urllib.parse.urlsplit(origin).netloc != request.headers.get("host", ""):
            return secure_response(
                JSONResponse(status_code=403, content={"detail": "Cross-origin request blocked"}),
                request,
            )

    if path not in PUBLIC_PATHS and not auth.get_user_id(request.cookies.get(SESSION_COOKIE, "")):
        # Browser navigation -> redirect to the sign-in page; XHR/curl -> 401 JSON.
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            target = RedirectResponse("/login", status_code=302)
        else:
            target = JSONResponse(status_code=401, content={"detail": "Authentication required"})
        return secure_response(target, request)

    response = await call_next(request)
    # Do not let the browser cache the sign-in form or app pages: otherwise a
    # signed-in user sees a stale /login (and Back after logout shows the app).
    if path == "/" or path == "/login" or path.endswith(".html"):
        response.headers["Cache-Control"] = "no-store"
    return secure_response(response, request)


@app.get("/api/health")
async def api_health():
    """Liveness probe for Render; public because health checks carry no cookie."""
    return {"status": "ok"}


# ── Models ───────────────────────────────────────────────────
class PlanRequest(BaseModel):
    lat: float = Field(..., description="Latitude", examples=[43.24])
    lon: float = Field(..., description="Longitude", examples=[76.89])
    crop: str = Field(
        ...,
        description="Crop: wheat, barley, corn, potato, rice",
        examples=["wheat"],
    )
    area_ha: float = Field(..., description="Field area, ha", examples=[10])
    l_per_h: float = Field(..., description="Fuel consumption, L/h", examples=[12])
    ha_per_h: float = Field(..., description="Work rate, ha/h", examples=[2])
    days: int = Field(3, description="Number of days to calculate (1–7)", examples=[3])


class DailyPlan(BaseModel):
    date: str = Field(..., description="Date, YYYY-MM-DD", examples=["2026-10-24"])
    et0_mm: float = Field(..., description="ET0 for the day, mm", examples=[1.6])
    rain_mm: float = Field(..., description="Rain for the day, mm", examples=[0.0])
    need_mm: float = Field(..., description="Water need for the day, mm", examples=[1.84])
    need_m3: float = Field(..., description="Water volume for the day, m³", examples=[184.0])


class PlanResponse(BaseModel):
    mm: float = Field(..., description="Total water need, mm", examples=[4.83])
    m3: float = Field(..., description="Total water volume, m³", examples=[483.0])
    diesel_l: float = Field(..., description="Diesel per pass, L", examples=[60.0])
    co2_kg: float = Field(..., description="CO2 from diesel, kg", examples=[159.0])
    kc: float = Field(..., description="Crop coefficient Kc (mid-season)", examples=[1.15])
    daily: list[DailyPlan] = Field(..., description="Day-by-day breakdown")


class AuthRequest(BaseModel):
    username: str = Field(..., description="Username: 3–32 characters, letters, digits, _", examples=["farmer"])
    password: str = Field(..., description="Password: at least 8 characters", examples=["field2026"])


class RegisterRequest(BaseModel):
    username: str = Field(..., description="Username: 3–32 characters, letters, digits, _", examples=["farmer"])
    password: str = Field(..., description="Password: at least 8 characters", examples=["field2026"])
    email: str = Field(..., description="Email to verify", examples=["farmer@example.com"])


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(..., description="Current password")
    new_password: str = Field(..., description="New password: at least 8 characters")


# Pydantic errors (e.g. text instead of a number) → 400 in Russian
@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc):
    return JSONResponse(status_code=400, content={"detail": "Please check the form data"})


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
                raise HTTPException(status_code=502, detail="Open-Meteo returned an invalid response")
            break
        except httpx.HTTPError:
            if attempt == 0:
                await asyncio.sleep(0.5)  # one quick retry, then give up
                continue
            raise HTTPException(status_code=502, detail="Open-Meteo weather service is not responding")

    daily = data.get("daily") if isinstance(data, dict) else None
    if not isinstance(daily, dict):
        raise HTTPException(status_code=502, detail="Open-Meteo returned incomplete data")

    dates = daily.get("time") or []
    et0 = daily.get("et0_fao_evapotranspiration") or []
    rain = daily.get("precipitation_sum") or []
    if not isinstance(dates, list) or not isinstance(et0, list) or not isinstance(rain, list):
        raise HTTPException(status_code=502, detail="Open-Meteo returned incomplete data")
    if len(dates) < days or len(et0) < days or len(rain) < days:
        raise HTTPException(status_code=502, detail="Open-Meteo returned incomplete data")

    dates, et0, rain = dates[:days], et0[:days], rain[:days]
    if any(not isinstance(value, (int, float)) for value in et0 + rain):
        raise HTTPException(status_code=502, detail="Open-Meteo returned incomplete data")

    result = (dates, et0, rain)
    WEATHER_CACHE[key] = (now + CACHE_TTL_SECONDS, result)
    return result


# ── POST /api/plan ───────────────────────────────────────────
@app.post(
    "/api/plan",
    response_model=PlanResponse,
    responses={
        400: {"description": "Invalid form data"},
        502: {"description": "Open-Meteo did not respond"},
    },
)
async def api_plan(req: PlanRequest, request: Request):
    # Optional hard gate: REQUIRE_VERIFIED_EMAIL=1 blocks calculations until
    # the account confirms its email address.
    if os.getenv("REQUIRE_VERIFIED_EMAIL") == "1":
        user_id = auth.get_user_id(request.cookies.get(SESSION_COOKIE, ""))
        if user_id and not auth.is_email_verified(user_id):
            raise HTTPException(status_code=403, detail="Verify your email to use the calculator")
    # Validation with clear Russian messages
    if req.crop not in calc.KC:
        raise HTTPException(status_code=400, detail="Unknown crop")
    if req.area_ha <= 0:
        raise HTTPException(status_code=400, detail="Field area must be greater than zero")
    if req.l_per_h <= 0:
        raise HTTPException(status_code=400, detail="Fuel consumption must be greater than zero")
    if req.ha_per_h <= 0:
        raise HTTPException(status_code=400, detail="Work rate must be greater than zero")
    if not (-90 <= req.lat <= 90 and -180 <= req.lon <= 180):
        raise HTTPException(status_code=400, detail="Coordinates out of range")
    if not (1 <= req.days <= 7):
        raise HTTPException(status_code=400, detail="Number of days must be between 1 and 7")

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


# ── Auth pages, register / login / logout ───────────────────
@app.get("/login")
async def login_page(request: Request):
    # Already signed in? No point in showing the form again.
    if auth.get_user_id(request.cookies.get(SESSION_COOKIE, "")):
        return RedirectResponse("/", status_code=302)
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/profile")
async def profile_page():
    return FileResponse(STATIC_DIR / "profile.html")


def _session_response(request: Request, payload: dict, token: str, status_code: int = 200):
    response = JSONResponse(content=payload, status_code=status_code)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=auth.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
        # Render terminates TLS and forwards X-Forwarded-Proto: https;
        # local dev stays plain http, where a Secure cookie would be dropped.
        secure=request.headers.get("x-forwarded-proto", "").lower() == "https",
    )
    return response


def _verification_url(request: Request, token: str) -> str:
    base = os.getenv("APP_BASE_URL", "").rstrip("/")
    if not base:
        proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
        base = f"{proto}://{request.headers.get('host', request.url.netloc)}"
    return f"{base}/verify?token={urllib.parse.quote(token)}"


def _client_key(request: Request, suffix: str = "") -> str:
    host = request.client.host if request.client else "unknown"
    return f"{host}:{suffix}" if suffix else host


@app.post("/api/auth/register", status_code=201)
async def api_register(req: RegisterRequest, request: Request):
    if rate_limited("register", _client_key(request), 30, 3600):
        raise HTTPException(status_code=429, detail="Too many sign-ups, try again later")
    try:
        user_id = auth.register_user(req.username, req.password, req.email)
        token = auth.create_email_token(user_id)
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    session = auth.create_session(user_id)
    profile = auth.get_profile(user_id) or {}
    verify_url = _verification_url(request, token)
    delivered = await asyncio.to_thread(
        mailer.send_verification_email,
        profile.get("email", ""),
        profile.get("username", ""),
        verify_url,
    )
    payload = {**profile, "email_sent": delivered}
    if not delivered and not mailer.smtp_configured():
        # Dev mode: no SMTP, so hand the single-use link back to this account.
        payload["verification_url"] = verify_url
    return _session_response(request, payload, session, status_code=201)


@app.post("/api/auth/login")
async def api_login(req: AuthRequest, request: Request):
    if rate_limited("login", _client_key(request, req.username.strip().lower()), 10, 300):
        raise HTTPException(status_code=429, detail="Too many attempts, try again in a few minutes")
    user_id = auth.verify_user(req.username, req.password)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Wrong username or password")
    token = auth.create_session(user_id)
    return _session_response(request, auth.get_profile(user_id) or {}, token)


@app.post("/api/auth/logout")
async def api_logout(request: Request):
    auth.delete_session(request.cookies.get(SESSION_COOKIE, ""))
    response = JSONResponse(content={"detail": "Signed out"})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/auth/me")
async def api_me(request: Request):
    user_id = auth.get_user_id(request.cookies.get(SESSION_COOKIE, ""))
    if user_id is None:  # middleware already blocks this; kept for clarity
        raise HTTPException(status_code=401, detail="Authentication required")
    return auth.get_profile(user_id) or {}


# ── Email verification and profile ──────────────────────────
@app.get("/api/profile")
async def api_profile(request: Request):
    user_id = auth.get_user_id(request.cookies.get(SESSION_COOKIE, ""))
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    profile = auth.get_profile(user_id) or {}
    return {**profile, "smtp_configured": mailer.smtp_configured()}


@app.get("/verify")
async def verify_email(token: str = ""):
    user_id = auth.consume_email_token(token)
    if user_id is None:
        return RedirectResponse("/login?verify=invalid", status_code=303)
    return RedirectResponse("/profile?verified=1", status_code=303)


@app.post("/api/auth/resend-verification")
async def resend_verification(request: Request):
    user_id = auth.get_user_id(request.cookies.get(SESSION_COOKIE, ""))
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if rate_limited("resend", str(user_id), 3, 600):
        raise HTTPException(status_code=429, detail="Too many emails, try again later")
    profile = auth.get_profile(user_id) or {}
    if profile.get("email_verified"):
        raise HTTPException(status_code=400, detail="Email is already verified")
    try:
        token = auth.create_email_token(user_id)
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    verify_url = _verification_url(request, token)
    delivered = await asyncio.to_thread(
        mailer.send_verification_email,
        profile.get("email", ""),
        profile.get("username", ""),
        verify_url,
    )
    if delivered:
        return {"detail": "Verification email sent", "email_sent": True}
    if mailer.smtp_configured():
        # SMTP is set up but refused the message: do not pretend the link is in the log.
        raise HTTPException(status_code=502, detail="Could not send the verification email, try again later")
    return {
        "detail": "Email delivery is not configured in this demo: use the link below.",
        "email_sent": False,
        "verification_url": verify_url,
    }


@app.post("/api/auth/password")
async def change_password(req: PasswordChangeRequest, request: Request):
    user_id = auth.get_user_id(request.cookies.get(SESSION_COOKIE, ""))
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if rate_limited("password", str(user_id), 5, 3600):
        raise HTTPException(status_code=429, detail="Too many attempts, try again later")
    if auth.verify_user(auth.get_username(user_id), req.current_password) != user_id:
        raise HTTPException(status_code=400, detail="Current password is wrong")
    try:
        auth.change_password(user_id, req.new_password)
    except auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # A password change revokes every session, then signs this browser in again.
    auth.delete_sessions_for_user(user_id)
    token = auth.create_session(user_id)
    return _session_response(request, {"detail": "Password updated"}, token)


# ── Static frontend (mount LAST so /api stays on top) ────────
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
