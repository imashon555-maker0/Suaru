# Suaru

**Demo:** ____________ — public Render link (fill in after deploying, see “Deploy on Render”).

A web app for farmers: drop a point on the map, pick a crop and machinery, and get
an estimate of how much water the field needs for the next days, how much diesel
will be burned, and how much CO₂ that releases.

Built at the AuaHack hackathon (Ecology track, Kazakhstan).

The app screen after signing in:

![Suaru app screen](docs/screenshot.png)

## What it shows

From a point on the map and the field settings:

- **water** — how many millimetres and cubic metres to irrigate over the chosen days;
- **per day** — date, rain and how much to irrigate that day;
- **diesel** — how many litres of fuel one field pass burns;
- **CO₂** — how many kilograms of carbon dioxide that diesel releases.

All numbers are computed on the server with the formulas below. This is an
**estimate**: the model is simplified and ignores soil type, crop growth stage and
irrigation losses.

## Formulas

- Daily water need, mm: `max(0, ET0 × Kc − rain)`, with ET0 and rain from Open-Meteo.
- Total water, mm: the sum of the daily need over the chosen days.
- Water volume: `m³ = mm × 10 × area_ha` (1 mm over 1 ha = 10 m³).
- Diesel: `L = area_ha × (L_per_h / ha_per_h)` — one machine pass.
- CO₂: `kg = litres × 2.65`.

Coefficient sources:

- `Kc` — FAO-56 “Crop evapotranspiration” (Allen et al., 1998), Table 12.
  `calc.py` uses the mid-season values: wheat 1.15, barley 1.15, corn 1.20,
  potato 1.15, rice 1.20.
  https://www.fao.org/4/x0490e/x0490e0b.htm
- `CO2_PER_L = 2.65` kg CO₂/L — derived from IPCC 2006 Guidelines, Vol. 2, Ch. 2,
  Table 2.2: 74.1 kg CO₂/TJ; net calorific value 43.0 TJ/Gg; density 0.832 kg/L.
  `74.1 × 43.0 / 1000 × 0.832 = 2.65`.
  https://www.ipcc-nggip.iges.or.jp/public/2006gl/pdf/2_Volume2/V2_2_Ch2_Stationary_Combustion.pdf

## Structure

```
main.py            # FastAPI: /api/plan, /api/health, /api/auth/*, /api/profile,
                   # /verify, request hardening, serves static/
auth.py            # accounts: SQLite, PBKDF2, sessions, email verification tokens
mailer.py          # verification emails over SMTP (stdlib) with a dev-mode log fallback
calc.py            # formulas: water, diesel, CO2
static/index.html  # Leaflet map, form, result card
static/login.html  # sign-in / sign-up page
static/profile.html# account, email verification and password change
test_calc.py       # formula checks
test_auth.py       # auth, verification, security and rate-limit checks (offline)
requirements.txt
render.yaml        # Render Blueprint: one-click deploy
docs/screenshot.png
README.md
```

## Run locally

```bash
python -m venv venv
venv\Scripts\activate        # Windows (PyCharm: select this interpreter)
source venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
uvicorn main:app --reload
```

Open http://127.0.0.1:8000/ — you will land on the sign-in page: create an account
(username 3–32 characters, email, password at least 8) and continue to the map.
API docs are at http://127.0.0.1:8000/docs (also behind the login).

Without SMTP configured, the verification email is not sent: the profile page
shows the single-use verification link directly (“Open the verification link”),
and the same link is printed to the server log. The flow therefore works locally
end to end.

Checks:

```bash
python test_calc.py
python test_auth.py
```

## Accounts, profile and email verification

- Sign-up requires a username (3–32 characters, letters, digits, `_`), an email and
  a password of at least 8 characters. The email is normalised to lower case and is
  unique per account.
- `/profile` shows the account: username, email and verification status, with a
  “Resend verification email” action while the address is unverified, plus a
  password change form. Changing a password revokes **every** session and signs
  this browser back in with a fresh cookie.
- Verification links are random, single-use tokens valid for 24 hours; the database
  stores only their SHA-256 digest. Opening `/verify?token=…` marks the email
  verified and lands on the profile page. A signed-in user with an unverified email
  sees a banner on the map pointing to the profile.
- Email is sent over SMTP (standard library, no extra packages). Any provider works:
  Resend, SendGrid, Mailgun, Brevo, or a Gmail app password. Configure it with the
  environment variables below. Until SMTP is configured the profile offers the link
  directly (demo mode) and the link is also written to the server log.
- Optional hard gate: with `REQUIRE_VERIFIED_EMAIL=1`, `POST /api/plan` answers
  `403` until the account confirms its email. Off by default so a demo never gets
  stuck on mail delivery.
- Accounts live in `suaru.db` next to the code (git-ignored). Render’s free plan has
  no persistent disk, so on every redeploy the database is recreated and accounts
  reset — users simply sign up again. Old databases are migrated in place on
  startup (email columns and the verification table are added automatically).
- Sign-up is open to anyone with the link. That is fine for a demo; for closed
  access, add an invite-code check in `auth.register_user`.

### Environment variables

| Variable | Purpose |
|---|---|
| `SMTP_HOST` | SMTP host; sending is enabled when it is set |
| `SMTP_PORT` | SMTP port, default `587` |
| `SMTP_USER` | SMTP login (optional for local relays) |
| `SMTP_PASSWORD` | SMTP password or API key |
| `SMTP_FROM` | From address, defaults to `SMTP_USER` |
| `SMTP_TLS` | `1` (default) uses STARTTLS |
| `APP_BASE_URL` | Public origin for links, e.g. `https://suaru.onrender.com`; derived from the request when unset |
| `REQUIRE_VERIFIED_EMAIL` | `1` blocks calculations until the email is verified |

No secrets belong in the repository: set these in Render → Environment (or your
local shell).

## API security

The app is a small demo, but the important protections are in place:

- **Auth gate** — every route except the sign-in page, `/verify`, `/api/health` and
  the auth endpoints requires a session: browsers get `302` to `/login`, API
  clients get `401`.
- **CSRF** — state-changing requests (`POST`/`PUT`/`PATCH`/`DELETE`) with a
  cross-site `Origin` header are refused with `403`; the session cookie is
  `SameSite=Lax` and `HttpOnly` (and `Secure` behind Render’s TLS proxy).
- **Headers** — `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, a Content-Security-Policy allowing only the app,
  Leaflet from unpkg and OSM tiles, plus HSTS when served over HTTPS.
- **Rate limits** — sign-in: 10 attempts per 5 minutes per IP+username; sign-up:
  30 per hour per IP; resend verification: 3 per 10 minutes; password change:
  5 per hour. Exceeding a limit returns `429`.
  The limiter is in memory, which matches a single Render instance.
- **Passwords** — PBKDF2-HMAC-SHA256, 390k iterations, per-user salt; comparison is
  constant-time.
- **Tokens** — session tokens and verification tokens are random, stored only as
  SHA-256 digests, and verification tokens are single-use with a 24-hour lifetime.
- **Sessions** — a week-long session cookie; logout deletes the session server-side;
  password change revokes all sessions.
- **Data** — SQLite via parameterised queries; connections are closed per request;
  no secrets are logged (the dev-mode verification link is the only exception and
  only when SMTP is unconfigured).

## API

`GET /api/health` → `{"status": "ok"}` — Render health check and a way to warm the
link up before a demo. Public, no cookie required.

`POST /api/plan`

```json
{
  "lat": 43.24,
  "lon": 76.89,
  "crop": "wheat",
  "area_ha": 10,
  "l_per_h": 12,
  "ha_per_h": 2,
  "days": 3
}
```

Example response (weather changes, so your numbers will differ):

```json
{
  "mm": 4.83,
  "m3": 483.0,
  "diesel_l": 60.0,
  "co2_kg": 159.0,
  "kc": 1.15,
  "daily": [
    { "date": "2026-10-24", "et0_mm": 1.6, "rain_mm": 0.0, "need_mm": 1.84, "need_m3": 184.0 },
    { "date": "2026-10-25", "et0_mm": 1.35, "rain_mm": 0.0, "need_mm": 1.55, "need_m3": 155.25 },
    { "date": "2026-10-26", "et0_mm": 1.25, "rain_mm": 0.0, "need_mm": 1.44, "need_m3": 143.75 }
  ]
}
```

Errors: `400` — invalid input, `401` — no session, `403` — cross-origin request or
unverified email with the gate on, `429` — rate limit, `502` — Open-Meteo did not
respond or returned incomplete data.

The contract is stable: `mm`, `m3`, `diesel_l`, `co2_kg`, `kc`, `daily[]` are never
renamed or removed. New fields are added additively, and clients must ignore
unknown fields.

Auth and profile:

| Endpoint | Purpose |
|---|---|
| `POST /api/auth/register` | `{username, password, email}` → `201`, signs you in, sends the verification link (returns it as `verification_url` in demo mode) |
| `POST /api/auth/login` | `{username, password}` → session cookie |
| `POST /api/auth/logout` | deletes the session server-side |
| `GET /api/auth/me` | `{username, email, email_verified}` |
| `GET /api/profile` | same, plus `smtp_configured` |
| `POST /api/auth/resend-verification` | new 24-hour verification link (max 3 per 10 minutes); returns `verification_url` in demo mode |
| `POST /api/auth/password` | `{current_password, new_password}`, revokes other sessions |
| `GET /verify?token=…` | single-use email confirmation |

## 60-second demo

Preparation 10 minutes before: open the link (the free plan wakes up in 1–2
minutes), check that the map is not grey, and have a demo account ready —
registering on stage wastes half a minute.

1. **Pin on the map** (15 sec): click once where the field is. “One click — and
   there are the field coordinates.”
2. **Estimate** (15 sec): check the form (wheat, 10 ha, 12 L/h, 2 ha/h, 3 days)
   and press “Calculate”. Show the total: water in mm and m³.
3. **Day-by-day calendar** (20 sec): walk through the rows — where it rains and
   where irrigation is needed. “If it rains tomorrow, irrigation drops — we don’t
   water into the rain.”
4. **Profile and /docs** (10 sec): open `/profile` (email, verification, password)
   and `/docs` — “the API is open and documented”.

Reference numbers to retell (wheat, 10 ha, 3 days): water ~5 mm ≈ 495 m³,
diesel 60 L, CO₂ 159 kg, Kc 1.15 (FAO-56). Exact mm depends on the weather.

Fallback if the internet or the map fails (use the demo account’s password):

```bash
curl -s -c /tmp/suaru-cookies.txt -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" -d '{"username": "demo", "password": "..."}'
curl -s -b /tmp/suaru-cookies.txt -X POST http://127.0.0.1:8000/api/plan \
  -H "Content-Type: application/json" \
  -d '{"lat": 43.24, "lon": 76.89, "crop": "wheat", "area_ha": 10, "l_per_h": 12, "ha_per_h": 2, "days": 3}'
```

The response carries the same mm/m3/diesel_l/co2_kg as the card on the map. If
Open-Meteo is down you get a `502` with an English message — that is honest
behaviour.

## Deploy on Render

Everything is described in [render.yaml](render.yaml) (Blueprint):

1. Push `main` to GitHub.
2. Render → New → Blueprint → connect the repository — the service is created from
   `render.yaml`: Python 3.12, build `pip install -r requirements.txt`,
   start `uvicorn main:app --host 0.0.0.0 --port $PORT`, health check
   `/api/health` (public, no cookie).
3. Add the email environment variables from the table above
   (Render → Environment) if you want real verification emails. Without them the
   link is written to the Render logs.
4. Put the public link into the “Demo” line at the top of this README.

If you don’t use the Blueprint, the same by hand:

1. Render → New → Web Service → connect the repository.
2. Build command: `pip install -r requirements.txt`
3. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Health check path: `/api/health`

FastAPI serves `static/` itself, so one service is enough.
The free plan sleeps after idle time, so warm the link up beforehand
(last item of the checklist).

## Post-deploy checklist

- [ ] The link opens on a phone: sign-in page, panel, form and result card are
      not clipped.
- [ ] A new user can sign up with an email and lands on the map; the verification
      banner is visible until the email is confirmed.
- [ ] With SMTP configured, the verification email arrives and its link marks the
      address verified; without SMTP the link appears in the Render logs.
- [ ] `/profile` shows username, email and status; resend works; a password change
      works and keeps you signed in on this device only.
- [ ] `GET /api/health` answers `200` and `{"status": "ok"}` without a cookie.
- [ ] `POST /api/plan` without a cookie answers `401`; with a cookie — `200`.
- [ ] `/docs` opens after signing in and “Try it out” works.
- [ ] Two identical requests in a row: the second answers instantly — weather comes
      from the 10-minute cache.
- [ ] Offline behaviour: turn Wi-Fi off, press “Calculate” — a clear English
      message; turn Wi-Fi on, press again — it works.
- [ ] Warm-up 5 minutes before the pitch: open the link and `/api/health` so the
      free plan wakes up.

## Manual browser checks (for the team)

1. **Console**: DevTools → Console — no red errors after load, sign-in, a map
   click and a calculation; Network → `POST /api/plan` — 200, and the numbers in
   the response match the card on screen.
2. **Sign in and out**: opening `/` without a session goes to `/login`;
   after signing in, `/login` sends you to the map; “Log out” returns to the form,
   and refreshing does not let you back in.
3. **Profile**: `/profile` shows the email and its status; “Resend verification
   email” reports where the link went (email or server log); changing the password
   logs out other devices.
4. **Mobile view**: DevTools → responsive 375×667 — the sign-in form and the panel
   are not clipped, and “Calculate” is reachable with a thumb.
5. **Map**: OpenStreetMap tiles loaded (not grey), a pin drops with one tap, zoom
   works, the “© OpenStreetMap” attribution is visible.
6. **Offline**: DevTools → Network → Offline → “Calculate” — an English error
   with a hint; turn Offline off → press again — it works.

## Model limitations

- `Kc` is mid-season only; growth stage and season are not modelled.
- Diesel is computed for a single machine pass.
- Soil type, irrigation equipment and irrigation losses are ignored.
- This is a planning estimate, not a precise agronomic forecast.

## Weather data

Weather: [Open-Meteo](https://open-meteo.com/) (CC BY 4.0), no API key.
The free tier is for non-commercial use only.
