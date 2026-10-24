# test_auth.py
# Login/registration checks. Run: python test_auth.py (no pytest, no internet:
# fetch_weather is replaced with a fixture).

import pathlib
import tempfile

import auth

# Throwaway DB for the tests; must be set before main is imported,
# because importing main creates the tables.
auth.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "test.db"

from fastapi.testclient import TestClient

import main

client = TestClient(main.app, follow_redirects=False)
COOKIE = "suaru_session"
PLAN = {
    "lat": 43.24,
    "lon": 76.89,
    "crop": "wheat",
    "area_ha": 10,
    "l_per_h": 12,
    "ha_per_h": 2,
    "days": 3,
}

# ── Registration ───────────────────────────────────────────
r = client.post("/api/auth/register", json={"username": "Fermer", "password": "pole2026"})
assert r.status_code == 201, r.text
assert client.cookies.get(COOKIE), "register must log you in right away"

r = client.post("/api/auth/register", json={"username": "fermer", "password": "pole2026"})
assert r.status_code == 400, "duplicate login (case-insensitive) must fail"

r = client.post("/api/auth/register", json={"username": "u2", "password": "123"})
assert r.status_code == 400, "short password must fail"

r = client.post("/api/auth/register", json={"username": "no", "password": "longenough"})
assert r.status_code == 400, "short username must fail"

# ── No cookie: login page is public, everything else is not ──
client.cookies.clear()
r = client.get("/login")
assert r.status_code == 200 and "Suaru" in r.text, "login page must be public"

r = client.get("/", headers={"Accept": "text/html"})
assert r.status_code == 302 and r.headers["location"] == "/login", "browser gets redirected"

r = client.post("/api/plan", json=PLAN)
assert r.status_code == 401, "API without session must be 401"

r = client.get("/api/auth/me")
assert r.status_code == 401, "/api/auth/me must require a session"

# ── Login ──────────────────────────────────────────────────
r = client.post("/api/auth/login", json={"username": "fermer", "password": "wrong"})
assert r.status_code == 401, "wrong password"

r = client.post("/api/auth/login", json={"username": "nobody", "password": "pole2026"})
assert r.status_code == 401, "unknown user"

r = client.post("/api/auth/login", json={"username": "Fermer", "password": "pole2026"})
assert r.status_code == 200 and r.json()["username"] == "fermer", r.text
assert client.cookies.get(COOKIE), "login must set the session cookie"

# ── Logged in: page, API and session info all work ────────
r = client.get("/")
assert r.status_code == 200, "index after login"


async def fake_weather(lat, lon, days):
    # 3-day fixture: no internet needed
    return (["2026-10-24", "2026-10-25", "2026-10-26"], [2.0, 1.5, 1.0], [0.0, 0.5, 0.0])


main.fetch_weather = fake_weather

r = client.post("/api/plan", json=PLAN)
assert r.status_code == 200, r.text
data = r.json()
assert data["kc"] == 1.15, "contract: kc unchanged"
assert data["diesel_l"] == 60.0, "contract: diesel unchanged"
assert len(data["daily"]) == 3 and "mm" in data and "m3" in data and "co2_kg" in data

r = client.get("/api/auth/me")
assert r.status_code == 200 and r.json()["username"] == "fermer"

# ── Logout closes the door behind you ──────────────────────
r = client.post("/api/auth/logout")
assert r.status_code == 200
assert not client.cookies.get(COOKIE), "logout must clear the cookie"

r = client.post("/api/plan", json=PLAN)
assert r.status_code == 401, "old session must stop working after logout"

print("OK: auth")
