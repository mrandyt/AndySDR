"""NaN / infinity must never reach the browser: JSON cannot carry them and JSON.parse rejects the whole message."""
import os, sys, json, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from andysdr import server, passlog

fails = []
def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name, "" if ok else extra)
    if not ok: fails.append(name)

inf, nan = float("inf"), float("nan")
s = server._dumps({"a": [1.5, nan, -inf, {"b": inf}], "c": (nan, 2), "d": "x"})
check("NaN / infinity become null, the rest is untouched", json.loads(s) == {"a": [1.5, None, None, {"b": None}], "c": [None, 2], "d": "x"}, s)
check("no 'NaN' or 'Infinity' word in the output", "NaN" not in s and "Infinity" not in s, s)
check("a clean message is dumped as normal JSON", server._dumps({"x": [1, 2.5, None]}) == '{"x": [1, 2.5, null]}')
m = passlog.PassMeter(lambda ts: (40.0, 900.0, 120.0)); t = time.time()
for i, v in enumerate([5.0, -inf, nan, 7.0, 8.0, 9.0, inf, 6.0, 5.0, 6.0, 7.0]): m.add(t + i * 2, v, 30.0)
su = m.summary(); txt = json.dumps(su)
check("a -inf / NaN / +inf reading is not recorded as a measurement", su["samples"] == 8 and "NaN" not in txt and "Infinity" not in txt, su)
print("ALL OK" if not fails else "FAILED: " + ", ".join(fails)); sys.exit(1 if fails else 0)
