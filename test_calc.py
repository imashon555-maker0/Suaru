# test_calc.py
# Simple checks for calc.py. Run: python test_calc.py

import calc

# 1 mm over 1 ha = 10 m3
assert calc.water_volume_m3(1.0, 1.0) == 10.0

# ET0 2 mm, no rain, Kc 1.15 -> 2.3 mm
assert round(calc.water_need_mm([2.0], [0.0], 1.15, 1), 2) == 2.3

# Rain above ET0 * Kc -> no water needed
assert calc.daily_water_mm(2.0, 10.0, 1.15) == 0.0

# 10 ha, 12 l/h, 2 ha/h -> 60 l
assert calc.diesel_liters(10, 12, 2) == 60.0

# 60 l * 2.65 kg/l = 159 kg
assert round(calc.co2_from_diesel(60), 2) == 159.0

print("OK: calc.py")
