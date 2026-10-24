# test_api.py
# Offline API checks for Suaru. Run: python test_api.py
# The weather source is replaced with a fixture, so no internet is needed.

import main
from fastapi.testclient import TestClient

# Fake forecast: three days, ET0 in mm, rain in mm.
DATES = ["2026-06-01", "2026-06-02", "2026-06-03"]
ET0 = [2.0, 1.5, 1.0]
RAIN = [0.0, 0.5, 0.0]

client = TestClient(main.app)
original_fetch_weather = main.fetch_weather


def fake_fetch_weather(lat, lon, days):
    """Return the fixture cut to the requested number of days."""
    return DATES[:days], ET0[:days], RAIN[:days]


async def async_fake_fetch_weather(lat, lon, days):
    return fake_fetch_weather(lat, lon, days)


def setup():
    """Replace the weather source and drop any cached weather."""
    main.fetch_weather = async_fake_fetch_weather
    main.WEATHER_CACHE.clear()


def teardown():
    main.fetch_weather = original_fetch_weather
    main.WEATHER_CACHE.clear()


def field(**changes):
    """A valid field, overridden by keyword arguments."""
    base = {
        "lat": 43.24,
        "lon": 76.89,
        "crop": "wheat",
        "stage": "mid",
        "area_ha": 10,
        "l_per_h": 12,
        "ha_per_h": 2,
        "days": 3,
    }
    base.update(changes)
    return base


def post(path, payload):
    response = client.post(path, json=payload)
    return response.status_code, response.json()


def test_health():
    assert client.get("/api/health").json() == {"status": "ok"}


def test_plan_uses_the_fixture():
    status, data = post("/api/plan", field())
    assert status == 200
    # Days: (2.0*1.15-0) + (1.5*1.15-0.5) + (1.0*1.15-0) = 4.675 mm over 10 ha = 467.5 m3
    assert data["mm"] == 4.67
    assert data["m3"] == 467.5
    assert data["diesel_l"] == 60.0
    assert data["co2_kg"] == 159.0
    assert data["stage"] == "mid" and data["kc"] == 1.15
    assert len(data["daily"]) == 3
    assert data["daily"][0]["date"] == DATES[0]
    assert data["daily"][1]["rain_mm"] == 0.5


def test_plan_stages_change_water_only():
    status, mid = post("/api/plan", field())
    status, ini = post("/api/plan", field(stage="ini"))
    status, end = post("/api/plan", field(stage="end"))
    assert (ini["kc"], mid["kc"], end["kc"]) == (0.30, 1.15, 0.25)
    assert ini["mm"] < mid["mm"]
    assert end["mm"] < ini["mm"]
    assert ini["diesel_l"] == mid["diesel_l"] == end["diesel_l"]


def test_plan_errors_in_russian():
    status, body = post("/api/plan", field(crop="banana"))
    assert status == 400 and body["detail"] == "Неизвестная культура"
    status, body = post("/api/plan", field(stage="seedling"))
    assert status == 400 and body["detail"] == "Неизвестная стадия роста"
    status, body = post("/api/plan", field(area_ha=0))
    assert status == 400 and body["detail"] == "Площадь поля должна быть больше нуля"
    status, body = post("/api/plan", field(lat=999))
    assert status == 400 and body["detail"] == "Координаты вне допустимого диапазона"
    status, body = post("/api/plan", field(area_ha="abc"))
    assert status == 400 and body["detail"] == "Проверьте данные формы"


def test_batch_totals_are_the_sum_of_fields():
    first = field(name="Поле у реки")
    second = field(name="Рис", crop="rice", stage="ini", area_ha=5)
    status, batch = post("/api/plan/batch", {"fields": [first, second]})
    assert status == 200
    assert [item["name"] for item in batch["fields"]] == ["Поле у реки", "Рис"]
    single_first = post("/api/plan", field())[1]
    single_second = post("/api/plan", field(crop="rice", stage="ini", area_ha=5))[1]
    assert batch["fields"][0]["plan"]["mm"] == single_first["mm"]
    assert batch["totals"]["area_ha"] == 15
    for key in ("m3", "diesel_l", "co2_kg"):
        assert batch["totals"][key] == round(single_first[key] + single_second[key], 2)
    assert "mm" not in batch["totals"]


def test_batch_without_names_numbers_the_fields():
    status, batch = post("/api/plan/batch", {"fields": [field(), field()]})
    assert status == 200
    assert [item["name"] for item in batch["fields"]] == ["Поле 1", "Поле 2"]


def test_batch_errors_name_the_field():
    status, body = post("/api/plan/batch", {"fields": []})
    assert status == 400 and body["detail"] == "Добавьте хотя бы одно поле"
    status, body = post("/api/plan/batch", {"fields": [field()] * 21})
    assert status == 400 and body["detail"] == "Слишком много полей (максимум 20)"
    status, body = post("/api/plan/batch", {"fields": [field(), field(crop="banana")]})
    assert status == 400 and body["detail"] == "Поле 2: Неизвестная культура"
    status, body = post(
        "/api/plan/batch", {"fields": [field(), field(name="Рис", area_ha=0)]}
    )
    assert status == 400
    assert body["detail"] == "Поле «Рис» (2): Площадь поля должна быть больше нуля"


def test_weather_failure_is_502():
    async def broken_fetch_weather(lat, lon, days):
        raise main.HTTPException(status_code=502, detail="Сервис погоды Open-Meteo не отвечает")

    main.fetch_weather = broken_fetch_weather
    status, body = post("/api/plan", field())
    assert status == 502 and body["detail"] == "Сервис погоды Open-Meteo не отвечает"
    main.fetch_weather = async_fake_fetch_weather


def run_all():
    tests = [
        test_health,
        test_plan_uses_the_fixture,
        test_plan_stages_change_water_only,
        test_plan_errors_in_russian,
        test_batch_totals_are_the_sum_of_fields,
        test_batch_without_names_numbers_the_fields,
        test_batch_errors_name_the_field,
        test_weather_failure_is_502,
    ]
    for test in tests:
        setup()
        try:
            test()
            print(f"OK: {test.__name__}")
        finally:
            teardown()
    print(f"OK: {len(tests)} API checks passed offline")


if __name__ == "__main__":
    run_all()
