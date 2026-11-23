"""DMR / P25 / NXDN / M17 Last heard: parsing DSD-FME's lines, one row per transmission, names from the callsign list."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from andysdr.lastheard import LastHeard, parse, describe

fails = []
def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok: fails.append(name)

class FakeDb:
    def __init__(self): self.people = {2356184: {"call": "M7FEP", "name": "Andy T", "place": "Bristol, United Kingdom"}}
    def lookup(self, i): return self.people.get(i)
    def tg_name(self, t): return {235: "United Kingdom", 91: "World-wide"}.get(t, "")

# ---------------------------------------------------------------- the parser on its own
ctx = {}
r = parse("SLOT 1 TGT=235 SRC=2356184 FLCO=0x00 FID=0x00 SVC=0x00 Group Call", ctx)
check("DMR group call with the payload fields", r and (r["proto"], r["slot"], r["src"], r["tgt"], r["kind"]) == ("DMR", 1, 2356184, 235, "group"))
r = parse("SLOT 2 TGT=2356000 SRC=2356184 Private Call", ctx)
check("DMR private call", r and r["kind"] == "private" and r["slot"] == 2)
r = parse("SLOT 1 TGT=2356000 SRC=2356184 FLCO=0x03 FID=0x00 SVC=0x00 ", ctx)
check("... also recognised from FLCO alone (unit-to-unit)", r and r["kind"] == "private")
r = parse("SLOT 1 TGT=9 SRC=2356184 Cap+ Group Emergency Encrypted Call", ctx)
check("Cap+, emergency and encrypted flags", r and r["emerg"] and r["enc"] and r["kind"] == "group")
check("a Hytera XPT call alert is signalling, not a voice call", parse("SLOT 1 FLCO=0x09 TGT=7 SRC=1234 Hytera XPT Group Call Alert", ctx) is None)
check("pseudo / data lines are ignored", parse(" SLOT 1 TGT=1 SRC=5 Encrypted Pseudo Call Information", ctx) is None
      and parse(" SLOT 1 TGT=1 SRC=5 Data Call", ctx) is None)
check("a zero ID is ignored", parse("SLOT 1 TGT=0 SRC=0 Group Call", ctx) is None)
check("ordinary chatter is ignored", parse("Sync: +DMR  [slot1]  slot2  | Color Code=03 | VLC", ctx) is None)
check("... but the colour code is remembered", ctx.get("cc") == "03")
r = parse("P25 LCW Group Voice Channel User - Group 1234 Source 5678", {})
check("P25 group voice", r and (r["proto"], r["tgt"], r["src"], r["kind"]) == ("P25", 1234, 5678, "group"))
r = parse("Unit to Unit Voice Channel User - Target 4321 Source 5678", {})
check("P25 unit to unit voice", r and r["kind"] == "private" and r["tgt"] == 4321)
r = parse("Group Call - Full Duplex Transmission (04) - Src=1234 - Dst/TG=567 ", {})
check("NXDN voice", r and (r["proto"], r["src"], r["tgt"], r["kind"]) == ("NXDN", 1234, 567, "group"))
r = parse("Individual Call - Src=1234 - Dst/TG=567 ", {})
check("NXDN individual call", r and r["kind"] == "private")
c = {}
r = parse(" CAN: 0 DST: ALL SRC: M7FEP", c)
check("M17 callsigns come from the stream", r and (r["proto"], r["src"], r["tgt"], r["kind"]) == ("M17", "M7FEP", "ALL", "group") and c["cc"] == "CAN 0")
r = parse(" CAN: 3 DST: G4ABC SRC: M7FEP Voice (3200bps)", {})
check("M17 to one station is a private call", r and r["kind"] == "private" and r["tgt"] == "G4ABC")
r = parse(" CAN: 0 DST: BROADCAST SRC: M7FEP", {})
check("M17 BROADCAST is a group call", r and r["kind"] == "group")
check("M17 with an unknown source is ignored", parse(" CAN: 0 DST: ALL SRC:  UNKNOWN FFFFFFFFFFFF", {}) is None)

# ---------------------------------------------------------------- the list
now = [5000.0]
pushed, logged = [], []
lh = LastHeard({}, lambda k, p: pushed.append((k, p)), FakeDb(), freq_fn=lambda: 438412500,
               on_new=lambda row, text: logged.append(text), clock=lambda: now[0], timers=False)
LINE = "SLOT 1 TGT=235 SRC=2356184 FLCO=0x00 FID=0x00 SVC=0x00 Group Call"
lh.feed("Sync: +DMR  [slot1]  slot2  | Color Code=01 | VLC")
lh.feed(LINE)
s = lh.snapshot()
check("the first burst makes one row", len(s) == 1 and s[0]["call"] == "M7FEP" and s[0]["name"] == "Andy T")
check("... with the talkgroup name, frequency and colour code", s[0]["tgname"] == "United Kingdom" and s[0]["freq"] == 438412500 and s[0]["cc"] == "01")
check("... pushed to the screens once", [k for k, _ in pushed] == ["heard"])
check("... and announced in the decoder log", logged == ["Heard: M7FEP Andy T (2356184) -> TG 235 United Kingdom  [DMR slot 1]"])
for i in range(1, 8):                       # bursts every 0.36 s for ~2.5 s
    now[0] += 0.36
    lh.feed(LINE)
s = lh.snapshot()
check("more bursts of the same call extend the row, not add one", len(s) == 1 and s[0]["n"] == 8 and s[0]["secs"] == 3 and s[0]["live"])
check("a running call is re-sent about once a second, not on every burst", 1 < len(pushed) <= 4)
lh.flush()
check("flush sends the final length", pushed[-1][1]["secs"] == 3)
now[0] += 10
check("10 s later the row is no longer live", lh.snapshot()[0]["live"] is False)
lh.feed(LINE)
check("the same station after a gap is a new transmission", len(lh.snapshot()) == 2 and len(logged) == 2)
lh.feed("SLOT 2 TGT=91 SRC=999999 Group Call")
s = lh.snapshot()
check("the other time slot is tracked separately and newest is first", len(s) == 3 and s[0]["src"] == 999999 and s[0]["slot"] == 2)
check("an unknown ID still gets a row without a name", s[0]["call"] == "" and s[0]["tgname"] == "World-wide")
lh.feed("SLOT 2 TGT=91 SRC=111111 Group Call")
check("a different person on the same slot starts a new row at once", len(lh.snapshot()) == 4)
lh.feed(" CAN: 0 DST: ALL SRC: M7FEP")
s = lh.snapshot()
check("M17 rows carry the callsign itself", s[0]["proto"] == "M17" and s[0]["call"] == "M7FEP" and s[0]["tgname"] == "")
check("... and read well in the log", logged[-1] == "Heard: M7FEP -> All  [M17]")

# names arriving later (the list finishes downloading after the call was heard)
lh.db.people[111111] = {"call": "G4XYZ", "name": "Sam", "place": "Leeds, United Kingdom"}
n = lh.relookup()
check("relookup fills in names for earlier rows", n == 1 and [r for r in lh.snapshot() if r["src"] == 111111][0]["call"] == "G4XYZ")
check("... and pushes the changed row", pushed[-1][1]["call"] == "G4XYZ")

# limits
for i in range(300):
    now[0] += 20
    lh.feed(f"SLOT 1 TGT=1 SRC={100000 + i} Group Call")
check("the list is capped at 200 rows", len(lh.snapshot()) == 200)
lh.clear()
check("clear empties it and tells every screen", lh.snapshot() == [] and pushed[-1] == ("heard_all", []))
lh.feed("SLOT 1 TGT=1 SRC=5 Group Call")
check("it works again after clearing", len(lh.snapshot()) == 1)
check("rows sent to screens have no private fields", all(not k.startswith("_") for k in pushed[-1][1]))
lh2 = LastHeard({}, lambda k, p: 1 / 0, None, clock=lambda: now[0], timers=False)
lh2.feed(LINE)
check("a failing screen or missing database never raises into the decoder", len(lh2.snapshot()) == 1 and lh2.snapshot()[0]["call"] == "")
check("describe() handles a private call", describe(dict(call="", name="", src=1, kind="private", tgt=2, tgname="", emerg=False, enc=True, proto="DMR", slot=1))
      == "Heard: ID (1) -> private call to 2 (encrypted)  [DMR slot 1]")

print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
