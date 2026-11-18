"""Self-test: EiBi parsing, day sets, 'on air now' (python tests/test_eibi.py)."""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from andysdr import eibi   # noqa: E402

ok = fail = 0


def check(name, cond):
    global ok, fail
    ok, fail = ok + bool(cond), fail + (not cond)
    print("  ok  " if cond else "  FAIL", name)


TXT = ("kHz:75;Time(UTC):93;Days:59;ITU:49;Station:201;Lang:49;Target:62;Remarks:135;P:35;Start:48;Stop:48\n"
       "5975;0000-0100;;D;Deutsche Welle;E;NAm;;1;;\n"
       "9410;2200-0200;Mo-Fr;USA;Radio Example;E;Eu;;1;;\n"
       "6070;0600-0900;Sa,Su;AUT;Weekend Radio;G;Eu;DRM;1;;\n"
       "7200;1000-1100;1245;D;Some Days;F;Af;;1;;\n")
rows = eibi.parse(TXT)
check("header skipped, 4 rows", len(rows) == 4)
check("fields", rows[0]["f"] == 5975 and rows[0]["st"] == "Deutsche Welle" and rows[0]["l"] == "E" and rows[0]["tg"] == "NAm")
check("days: Mo-Fr / Sa,Su / 1245 / empty", eibi.days_set("Mo-Fr") == {0, 1, 2, 3, 4} and eibi.days_set("Sa,Su") == {5, 6}
      and eibi.days_set("1245") == {0, 1, 3, 4} and eibi.days_set("") is None)
wed = dt.datetime(2026, 11, 18, 0, 30, tzinfo=dt.timezone.utc)      # Wednesday 00:30 UTC
check("daily 0000-0100 on air at 00:30", eibi.on_air(rows[0], wed))
check("wrapping 2200-0200 Mo-Fr: Wed 00:30 is Tue's slot -> on", eibi.on_air(rows[1], wed))
mon = dt.datetime(2026, 11, 16, 0, 30, tzinfo=dt.timezone.utc)      # Monday 00:30 -> belongs to Sunday's slot -> off
check("wrapping slot: Mon 00:30 belongs to Sunday -> off", not eibi.on_air(rows[1], mon))
check("wrapping slot: Wed 23:00 on", eibi.on_air(rows[1], dt.datetime(2026, 11, 18, 23, 0, tzinfo=dt.timezone.utc)))
sat = dt.datetime(2026, 11, 21, 7, 0, tzinfo=dt.timezone.utc)
check("Sa,Su 0600-0900 on Saturday 07:00", eibi.on_air(rows[2], sat) and not eibi.on_air(rows[2], wed))
check("season codes", eibi.season_code(dt.date(2026, 11, 18)) == "b26" and eibi.season_code(dt.date(2026, 5, 1)) == "a26"
      and eibi.season_code(dt.date(2027, 2, 1)) == "b26" and eibi.season_code(dt.date(2026, 10, 24)) == "a26"
      and eibi.season_code(dt.date(2026, 10, 25)) == "b26")
import tempfile
os.environ["HOME"] = tempfile.mkdtemp(); os.environ.pop("APPDATA", None)
e = eibi.Eibi()
e.rows = sorted(rows, key=lambda r: r["f"])
q = e.query(now_only=True, now_utc=wed)
check("query on air now", {r["st"] for r in q["rows"]} == {"Deutsche Welle", "Radio Example"} and q["total"] == 2)
check("query text + freq range", e.query(q="weekend")["total"] == 1 and e.query(fmin=7000, fmax=8000)["total"] == 1)
print(f"{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
