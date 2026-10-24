# test_auth.py
# Auth checks: sign-up, sign-in, email verification, profile, password change,
# request hardening and rate limits. Run: python test_auth.py
# No pytest and no internet: fetch_weather and the mailer are replaced.

import os
import pathlib
import tempfile
import urllib.parse

import auth

# Throwaway DB; must be set before main is imported (import creates the tables).
auth.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "test.db"

from fastapi.testclient import TestClient

import main
import mailer

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

# Capture verification links instead of talking to an SMTP server.
SENT = []


def fake_mailer(to_email, username, verify_url):
    SENT.append(verify_url)
    return True


mailer.send_verification_email = fake_mailer


def token_from(url: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["token"][0]


# ── Anonymous: public surface and headers ───────────────────
r = client.get("/login")
assert r.status_code == 200 and "Suaru" in r.text
assert r.headers["x-content-type-options"] == "nosniff"
assert r.headers["x-frame-options"] == "DENY"
assert "default-src 'self'" in r.headers["content-security-policy"]

r = client.get("/api/health")
assert r.status_code == 200 and r.json() == {"status": "ok"}, \
    "health check must be public: Render probes it without a cookie"

r = client.post("/api/plan", json=PLAN)
assert r.status_code == 401, "API without a session must be 401"

r = client.get("/api/auth/me")
assert r.status_code == 401

r = client.get("/docs", headers={"Accept": "text/html"})
assert r.status_code == 302 and r.headers["location"] == "/login"

# CSRF: a cross-site Origin on a mutation is refused
r = client.post(
    "/api/auth/login",
    json={"username": "fermer", "password": "pole2026"},
    headers={"Origin": "https://evil.example"},
)
assert r.status_code == 403, "cross-origin POST must be blocked"

# ── Registration validation ─────────────────────────────────
r = client.post("/api/auth/register", json={"username": "fermer", "password": "pole2026", "email": "not-an-email"})
assert r.status_code == 400 and "email" in r.json()["detail"].lower()

r = client.post("/api/auth/register", json={"username": "fermer", "password": "short", "email": "farmer@example.com"})
assert r.status_code == 400, "password shorter than 8 must fail"

r = client.post("/api/auth/register", json={"username": "no", "password": "pole2026", "email": "farmer@example.com"})
assert r.status_code == 400, "username shorter than 3 must fail"

# ── Registration and profile ────────────────────────────────
r = client.post("/api/auth/register", json={"username": "Fermer", "password": "pole2026", "email": "Farmer@Example.com"})
assert r.status_code == 201, r.text
assert "verification_url" not in r.json(), "a delivered email must not hand out the link"
assert r.json()["username"] == "fermer"
assert r.json()["email"] == "farmer@example.com", "email must be normalised"
assert r.json()["email_verified"] is False
assert r.json()["email_sent"] is True
assert client.cookies.get(COOKIE), "registration must sign you in"
assert len(SENT) == 1, "the verification email must be triggered"

r = client.get("/api/profile")
assert r.status_code == 200
profile = r.json()
assert profile["email"] == "farmer@example.com" and profile["email_verified"] is False

# ── Email verification ──────────────────────────────────────
verify_url = SENT[-1]
r = client.get(urllib.parse.urlsplit(verify_url).path + "?" + urllib.parse.urlsplit(verify_url).query)
assert r.status_code == 303 and r.headers["location"] == "/profile?verified=1"

r = client.get("/api/profile")
assert r.json()["email_verified"] is True, "email must be verified after the link"

# The token is single-use
r = client.get("/verify", params={"token": token_from(verify_url)})
assert r.status_code == 303 and r.headers["location"] == "/login?verify=invalid"

r = client.post("/api/auth/resend-verification")
assert r.status_code == 400 and "already verified" in r.json()["detail"].lower()

# ── Weather-stubbed plan keeps the v1 contract ──────────────
async def fake_weather(lat, lon, days):
    return (["2026-10-24", "2026-10-25", "2026-10-26"], [2.0, 1.5, 1.0], [0.0, 0.5, 0.0])


main.fetch_weather = fake_weather

r = client.post("/api/plan", json=PLAN)
assert r.status_code == 200, r.text
data = r.json()
assert data["kc"] == 1.15 and data["diesel_l"] == 60.0
assert len(data["daily"]) == 3 and "mm" in data and "m3" in data and "co2_kg" in data

# ── Password change revokes sessions ────────────────────────
old_token = client.cookies.get(COOKIE)

r = client.post("/api/auth/password", json={"current_password": "wrong-one", "new_password": "newpole2026"})
assert r.status_code == 400 and "current password" in r.json()["detail"].lower()

r = client.post("/api/auth/password", json={"current_password": "pole2026", "new_password": "short"})
assert r.status_code == 400, "new password shorter than 8 must fail"

r = client.post("/api/auth/password", json={"current_password": "pole2026", "new_password": "newpole2026"})
assert r.status_code == 200, r.text
new_token = client.cookies.get(COOKIE)
assert new_token and new_token != old_token, "the session must be rotated"

old_client = TestClient(main.app, follow_redirects=False)
r = old_client.post("/api/plan", json=PLAN, headers={"Cookie": f"{COOKIE}={old_token}"})
assert r.status_code == 401, "the old session must be revoked"

r = client.post("/api/auth/login", json={"username": "fermer", "password": "pole2026"})
assert r.status_code == 401, "the old password must stop working"

r = client.post("/api/auth/login", json={"username": "fermer", "password": "newpole2026"})
assert r.status_code == 200, "the new password must work"

# ── Duplicates ──────────────────────────────────────────────
r = client.post("/api/auth/register", json={"username": "fermer", "password": "pole2026", "email": "other@example.com"})
assert r.status_code == 400, "duplicate username must fail"

r = client.post("/api/auth/register", json={"username": "fermer2", "password": "pole2026", "email": "farmer@example.com"})
assert r.status_code == 400 and "email" in r.json()["detail"].lower(), "duplicate email must fail"

# ── Optional hard gate: REQUIRE_VERIFIED_EMAIL=1 ────────────
os.environ["REQUIRE_VERIFIED_EMAIL"] = "1"
try:
    r = client.post("/api/auth/register", json={"username": "gateuser", "password": "pole2026", "email": "gate@example.com"})
    assert r.status_code == 201, r.text
    assert client.cookies.get(COOKIE)

    r = client.post("/api/plan", json=PLAN)
    assert r.status_code == 403, "unverified accounts must be blocked when the gate is on"

    gate_url = SENT[-1]
    client.get(urllib.parse.urlsplit(gate_url).path + "?" + urllib.parse.urlsplit(gate_url).query)
    r = client.post("/api/plan", json=PLAN)
    assert r.status_code == 200, "verified accounts must pass the gate"
finally:
    os.environ.pop("REQUIRE_VERIFIED_EMAIL", None)

# ── Dev mode gives the link to the owner; a broken SMTP does not ──
mailer.send_verification_email = lambda *args: False  # no provider in this demo
r = client.post(
    "/api/auth/register",
    json={"username": "smtpfail", "password": "pole2026", "email": "smtpfail@example.com"},
)
assert r.status_code == 201 and r.json()["email_sent"] is False
assert r.json()["verification_url"].startswith("http"), "register must return the dev link"

r = client.post("/api/auth/resend-verification")
assert r.status_code == 200
assert r.json()["verification_url"].startswith("http"), "resend must return the dev link"

os.environ["SMTP_HOST"] = "smtp.invalid"
try:
    r = client.post("/api/auth/resend-verification")
    assert r.status_code == 502, r.text
    assert "verification_url" not in r.json(), "no link when SMTP is configured but failing"
    assert "log" not in r.json()["detail"].lower(), "must not claim the link is in the log"
finally:
    os.environ.pop("SMTP_HOST", None)
    mailer.send_verification_email = fake_mailer

# ── Logout ──────────────────────────────────────────────────
r = client.post("/api/auth/logout")
assert r.status_code == 200
assert not client.cookies.get(COOKIE)
r = client.post("/api/plan", json=PLAN)
assert r.status_code == 401

# ── Login rate limit ────────────────────────────────────────
statuses = []
for _ in range(11):
    r = client.post("/api/auth/login", json={"username": "spamtry", "password": "whatever"})
    statuses.append(r.status_code)
assert statuses[:10] == [401] * 10, statuses
assert statuses[10] == 429, "the 11th attempt must be rate-limited"

print("OK: auth")
