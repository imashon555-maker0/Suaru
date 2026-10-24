# calc.py
# Formulas for Suaru. All results are simplified estimates.

# Crop coefficients (Kc) for the mid-season stage.
# Source: FAO-56 "Crop evapotranspiration" (Allen et al., 1998), Table 12.
# https://www.fao.org/4/x0490e/x0490e0b.htm
# Checked against Table 12: wheat 1.15, barley 1.15, maize 1.20,
# potato 1.15, rice 1.20.
KC = {
    "wheat": 1.15,
    "barley": 1.15,
    "corn": 1.20,
    "potato": 1.15,
    "rice": 1.20,
}

# Diesel emission factor: kg CO2 per liter of diesel.
# Source: IPCC 2006 Guidelines, Vol. 2, Ch. 2, Table 2.2: 74.1 kg CO2/TJ.
# Net calorific value of gas/diesel oil: 43.0 TJ/Gg; density: 0.832 kg/l.
# 74.1 * 43.0 / 1000 * 0.832 = 2.65 kg CO2/l.
# Density example: UNFCCC, Energy Sector (2016), "density of diesel 0.832 kg/litres".
CO2_PER_L = 2.65


def daily_water_mm(et0_mm, rain_mm, kc):
    """Water need for one day: max(0, ET0 * Kc - rain)."""
    return max(0.0, et0_mm * kc - rain_mm)


def water_need_mm(et0_values, precipitation_values, kc, days):
    """Sum the daily water need over `days` days."""
    total = 0.0
    for i in range(min(days, len(et0_values), len(precipitation_values))):
        total += daily_water_mm(et0_values[i], precipitation_values[i], kc)
    return total


def water_volume_m3(mm, area_ha):
    """1 mm of water over 1 ha = 10 m3."""
    return mm * 10 * area_ha


def diesel_liters(area_ha, l_per_h, ha_per_h):
    """Area / speed = hours of work; hours * liters per hour = liters."""
    return area_ha * (l_per_h / ha_per_h)


def co2_from_diesel(liters):
    """CO2 emitted by burning `liters` of diesel."""
    return liters * CO2_PER_L
