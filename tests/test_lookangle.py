"""Look angles to geostationary satellites (the 'Where to point' tool)."""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from andysdr.lookangle import GEO_SATS, GEO_RADIUS_KM, compass, geo_look, geo_table  # noqa: E402
from andysdr.server import Server  # noqa: E402


def textbook(lat, lon, sat_lon):
    """Spherical-earth formulas from the satellite-TV literature (independent of the ECEF code)."""
    phi, dl = math.radians(lat), math.radians(sat_lon - lon)
    g = math.acos(math.cos(phi) * math.cos(dl))
    el = math.degrees(math.atan2(math.cos(g) - 6378.137 / GEO_RADIUS_KM, math.sin(g)))
    a = math.degrees(math.atan(math.tan(abs(dl)) / math.sin(abs(phi))))
    if phi >= 0:
        az = 180 - a if dl > 0 else 180 + a        # northern hemisphere: east satellites are SE of south
    else:
        az = a if dl > 0 else 360 - a              # southern hemisphere: look north
    return az, el


class Geo(unittest.TestCase):
    def test_matches_textbook_formulas(self):
        pts = [(52.668, -2.472), (51.5, -0.13), (40.7, -74.0), (35.7, 139.7), (-33.9, 151.2), (64.1, -21.9), (10.0, 30.0)]
        for lat, lon in pts:
            for sl in (-98.4, -15.5, 0.0, 24.9, 64.0, 143.5, 76.0):
                r = geo_look(lat, lon, sl, 0.0)
                az, el = textbook(lat, lon, sl)
                if el < 1.0:                   # the spherical shortcut is not meaningful at the horizon
                    continue
                self.assertAlmostEqual(r["el"], el, delta=0.35, msg=(lat, lon, sl))
                d = abs((r["az"] - az + 180) % 360 - 180)
                self.assertLess(d, 0.35, msg=(lat, lon, sl, r["az"], az))

    def test_shropshire_alphasat(self):
        r = geo_look(52.668, -2.472, 24.9, 120)
        self.assertAlmostEqual(r["az"], 146.9, delta=0.3)
        self.assertAlmostEqual(r["el"], 24.7, delta=0.3)
        self.assertEqual(r["compass"], "SSE")
        self.assertTrue(r["visible"])
        self.assertAlmostEqual(r["tilt"], 90 - r["el"], places=1)

    def test_special_cases(self):
        r = geo_look(0.0, 25.0, 25.0, 0.0)                    # straight under the satellite
        self.assertAlmostEqual(r["el"], 90.0, delta=0.1)
        self.assertAlmostEqual(r["range_km"], GEO_RADIUS_KM - 6378.137, delta=1)
        r = geo_look(52.0, 10.0, 10.0, 0.0)                    # same longitude: due south
        self.assertAlmostEqual(r["az"], 180.0, delta=0.05)
        r = geo_look(-35.0, 10.0, 10.0, 0.0)                   # southern hemisphere: due north
        self.assertTrue(r["az"] < 0.05 or r["az"] > 359.95)
        # mirror-image longitudes give mirror-image bearings
        a, b = geo_look(50.0, 0.0, 30.0)["az"], geo_look(50.0, 0.0, -30.0)["az"]
        self.assertAlmostEqual(a + b, 360.0, delta=0.1)

    def test_horizon_and_below(self):
        r = geo_look(52.668, -2.472, 76.0, 0.0)                # Elektro-L3 today: below the UK horizon
        self.assertFalse(r["visible"])
        self.assertLess(r["el"], 0)
        r = geo_look(52.668, -2.472, -75.2, 0.0)               # GOES-19: a degree or two up
        self.assertTrue(r["visible"])
        self.assertLess(r["el"], 3.0)
        r = geo_look(52.668, -2.472, 177.5, 0.0)               # the far side of the world
        self.assertFalse(r["visible"])

    def test_bad_position(self):
        with self.assertRaises(ValueError):
            geo_look(95.0, 0.0, 25.0)
        with self.assertRaises(ValueError):
            geo_look(0.0, 400.0, 25.0)

    def test_compass_names(self):
        for az, name in ((0, "N"), (359, "N"), (146.9, "SSE"), (196.2, "SSW"), (90, "E"), (225, "SW"), (270, "W")):
            self.assertEqual(compass(az), name, az)

    def test_table(self):
        rows = geo_table(52.668, -2.472, 120.0)
        self.assertEqual(len(rows), len(GEO_SATS))
        self.assertEqual(rows[0]["id"], "alphasat")                         # list order kept, visible ones first
        seen_hidden = False
        for r in rows:                                                      # never a visible row after a hidden one
            seen_hidden = seen_hidden or not r["visible"]
            self.assertFalse(seen_hidden and r["visible"])
        rows = geo_table(52.668, -2.472, 120.0, extra_lon=-30.0)
        self.assertEqual(rows[0]["id"], "custom")
        self.assertIn("30.0 W", rows[0]["name"])
        self.assertEqual(len(rows), len(GEO_SATS) + 1)


class FakeSats:
    def __init__(self, obs):
        self._o = obs

    def observer(self):
        return self._o


class FakeRadio:
    def __init__(self, obs):
        self.scheduler = type("S", (), {"sats": FakeSats(obs)})()


class ServerReply(unittest.TestCase):
    def test_needs_a_position(self):
        out = Server._geo_look(FakeRadio(None))
        self.assertIn("position", out["error"])

    def test_rows_for_saved_position(self):
        out = Server._geo_look(FakeRadio((52.668, -2.472, 120.0)))
        self.assertEqual(out["observer"], [52.668, -2.472])
        self.assertTrue(any(r["id"] == "alphasat" and r["visible"] for r in out["rows"]))

    def test_custom_longitude(self):
        out = Server._geo_look(FakeRadio((52.668, -2.472, 120.0)), "-15.5")
        self.assertEqual(out["rows"][0]["id"], "custom")
        self.assertAlmostEqual(out["rows"][0]["az"], 196.2, delta=0.3)

    def test_bad_longitude(self):
        for bad in ("abc", "999", "-181"):
            self.assertIn("longitude", Server._geo_look(FakeRadio((52.0, -2.0, 30.0)), bad)["error"])
        # empty means "no extra row", not an error
        self.assertNotIn("error", Server._geo_look(FakeRadio((52.0, -2.0, 30.0)), ""))


if __name__ == "__main__":
    unittest.main()
