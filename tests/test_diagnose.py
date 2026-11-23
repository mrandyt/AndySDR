"""CHECK MY SYSTEM: every check on fake radios / fake Windows answers, the firewall parser, and the diagnostics zip."""
import os, sys, io, json, zipfile, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from andysdr import diagnose as D

fails = []
def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok: fails.append(name)

class Dev:
    def __init__(self, kind, label="RSP2pro", rate=2_000_000): self.kind, self.label, self.sample_rate = kind, label, rate
class Dec:
    def __init__(self, ready): self.r = ready
    def available(self): return [{"id": k, "name": k.upper(), "ready": v} for k, v in self.r.items()]
class Radio:
    def __init__(self, dev, devs=(), ready=None, overload=0.0, bias="ok"):
        self.dev, self.dev_list = dev, list(devs)
        self.decoders = Dec(ready or {"dsd": True, "pager": True})
        self._o, self._b = overload, bias
        class Sats:
            def cfg(s): return {}
            def observer(s): return None
        class Sch: sats = Sats()
        self.scheduler = Sch()
    def bias_snapshot(self): return {"overload_rate": self._o, "state": self._b}
def ids(items, level=None): return {i["id"]: i["level"] for i in items}

# ---------------------------------------------------------------- receiver
r = D.check_receiver(Radio(Dev("sdrplay", "SDRplay RSP2pro 1234")))
check("a real receiver in use is OK", r[0]["level"] == "ok" and "RSP2pro" in r[0]["title"] and "2.00 MS/s" in r[0]["detail"])
r = D.check_receiver(Radio(Dev("sim", "Simulator"), [Dev("sdrplay", "RSP2pro")]))
check("found but not open (another program has it) is a warning that names it", r[0]["level"] == "warn" and "RSP2pro" in r[0]["detail"] and "another program" in r[0]["fix"].lower())
r = D.check_receiver(Radio(Dev("sim"), [Dev("sim")]))
check("nothing found is a problem with the next steps", r[0]["level"] == "bad" and "Zadig" in r[0]["fix"])

# ---------------------------------------------------------------- SDRplay driver + Windows service
class FakeSp:
    result = {"available": True, "version": 3.15}; loaded = True
    def api_status(self): return FakeSp.result
    def _load(self): return FakeSp.loaded
import andysdr.sdrplay as real_sp
orig = (real_sp.api_status, real_sp._load)
real_sp.api_status = lambda: FakeSp.result
real_sp._load = lambda: FakeSp.loaded
def sc(out, rc=0): return lambda args, timeout=6: (rc, out)
RUN = "SERVICE_NAME: SDRplayAPIService\n        TYPE               : 10  WIN32_OWN_PROCESS\n        STATE              : 4  RUNNING\n"
r = D.check_sdrplay(Radio(Dev("sim")), sc(RUN), windows=True)
check("API answering and service running", ids(r) == {"sdrplay_api": "ok", "sdrplay_service": "ok"})
r = D.check_sdrplay(Radio(Dev("sim")), sc(RUN.replace("4  RUNNING", "1  STOPPED")), windows=True)
check("a stopped service is a problem with the exact command to start it", ids(r)["sdrplay_service"] == "bad" and r[-1]["cmd"] == "net start SDRplayAPIService")
r = D.check_sdrplay(Radio(Dev("sim")), sc("[SC] EnumQueryServicesStatus:OpenService FAILED 1060:\nThe specified service does not exist as an installed service.\n", 1060), windows=True)
check("service not installed while the API works is a warning", ids(r)["sdrplay_service"] == "warn")
FakeSp.result = {"available": False, "error": "SDRplay API not installed"}; FakeSp.loaded = False
r = D.check_sdrplay(Radio(Dev("sim")), sc("", 1060), windows=True)
check("no API at all is explained as only needed for RSPs and says where to get it", ids(r)["sdrplay_api"] == "info" and "sdrplay.com" in r[0]["fix"])
FakeSp.result = {"available": False, "error": "Open failed: service not running"}; FakeSp.loaded = True
r = D.check_sdrplay(Radio(Dev("sim")), sc(RUN.replace("4  RUNNING", "1  STOPPED")), windows=True)
check("DLL present but API will not open is a problem", ids(r)["sdrplay_api"] == "bad")
FakeSp.result = {"available": True, "version": 3.02}; FakeSp.loaded = True
r = D.check_sdrplay(Radio(Dev("sdrplay")), sc(RUN), windows=True)
check("an old API version is a warning", ids(r)["sdrplay_api"] == "warn")
r = D.check_sdrplay(Radio(Dev("sim")), lambda a, timeout=6: (None, ""), windows=True)
check("if Windows cannot be asked, it says so and carries on", ids(r)["sdrplay_service"] == "info")
r = D.check_sdrplay(Radio(Dev("sim")), sc(""), windows=False)
check("no Windows service check on other systems", "sdrplay_service" not in ids(r))
real_sp.api_status, real_sp._load = orig

# ---------------------------------------------------------------- overload / bias
check("overloads are flagged", ids(D.check_signal(Radio(Dev("sdrplay"), overload=12)))["overload"] == "warn")
check("no overloads is fine", ids(D.check_signal(Radio(Dev("sdrplay"), overload=0)))["overload"] == "ok")
check("lost bias-T power is flagged", ids(D.check_signal(Radio(Dev("sdrplay"), bias="drop")))["bias"] == "warn")
check("the simulator is not judged", D.check_signal(Radio(Dev("sim"), overload=50)) == [])

# ---------------------------------------------------------------- decoders
check("all decoders present", D.check_decoders(Radio(Dev("sim"), ready={"a": True, "b": True}))[0]["level"] == "ok")
x = D.check_decoders(Radio(Dev("sim"), ready={"dsd": False, "pager": True, "sstv": False}))[0]
check("missing decoders are listed with the fix", x["level"] == "warn" and "DSD" in x["detail"] and "SSTV" in x["detail"] and "Download missing" in x["fix"])

# ---------------------------------------------------------------- firewall parser
ALLOW = """
Rule Name:                            AndySDR
----------------------------------------------------------------------
Enabled:                              Yes
Direction:                            In
Profiles:                             Private
Protocol:                             TCP
LocalPort:                            8045
Action:                               Allow

Rule Name:                            Something else
----------------------------------------------------------------------
Enabled:                              Yes
Protocol:                             UDP
LocalPort:                            53
Action:                               Allow
"""
PROG = """Rule Name:                            AndySDR.exe
----------------------------------------------------------------------
Enabled:                              Yes
Direction:                            In
Protocol:                             Any
LocalPort:                            Any
Program:                              C:\\Users\\Andy\\AppData\\Local\\AndySDR\\AndySDR.exe
Action:                               Allow
"""
OTHER = ALLOW.split("Rule Name:                            Something")[1]
OTHER = "Rule Name:   Something" + OTHER
check("an allow rule for port 8045", D.parse_firewall(ALLOW, 8045) is True)
check("an allow rule for the program (the one Windows makes when asked)", D.parse_firewall(PROG, 8045) is True)
check("other rules only -> blocked", D.parse_firewall(OTHER, 8045) is False)
check("... exactly: disabled AndySDR rule + nothing else", D.parse_firewall(PROG.replace("Yes", "No"), 8045) is False)
check("a Block rule (clicked Cancel) beats an allow rule", D.parse_firewall(PROG + "\n" + PROG.replace("Allow", "Block").replace("AndySDR.exe\n---", "AndySDR.exe block\n---"), 8045) is False)
check("another language / no rules -> cannot tell", D.parse_firewall("Aucune règle ne correspond aux critères spécifiés.", 8045) is None and D.parse_firewall("", 8045) is None)
check("port in a list of ports", D.parse_firewall(ALLOW.replace("8045", "80, 8045, 9000"), 8045) is True)

# ---------------------------------------------------------------- network
S = {"server_host": "0.0.0.0", "server_port": 8045, "password": "ANDY-1A2B3C"}
fw = lambda txt, rc=0: (lambda args, timeout=6: (rc, txt))
r = D.check_network(S, ["192.168.1.20"], fw(ALLOW), windows=True)
check("remote on + good password + firewall open -> all OK and shows the address", ids(r) == {"remote": "ok", "password": "ok", "firewall": "ok"} and "192.168.1.20:8045" in r[0]["detail"])
r = D.check_network(S, [], fw(OTHER), windows=True)
fwi = [i for i in r if i["id"] == "firewall"][0]
check("a firewall that is not open gives the exact netsh command for this port", fwi["level"] == "warn" and "localport=8045" in fwi["cmd"] and "profile=private" in fwi["cmd"])
r = D.check_network(dict(S, password="abc"), [], fw(ALLOW), windows=True)
check("a short password is a warning", ids(r)["password"] == "warn")
r = D.check_network(dict(S, server_host="127.0.0.1"), [], fw(ALLOW), windows=True)
check("remote access off is just information, with no password / firewall noise", ids(r) == {"remote": "info"})
r = D.check_network(S, [], fw("", None), windows=True)
check("an unreadable firewall is information, not a failure", ids(r)["firewall"] == "info")
r = D.check_network(S, [], fw(ALLOW), windows=False)
check("no firewall check off Windows", "firewall" not in ids(r))

# ---------------------------------------------------------------- the whole report survives a broken check
rep = D.run_checks(Radio(Dev("sdrplay")), dict(S), ["10.0.0.5"], run=fw(ALLOW), windows=False)
check("run_checks returns items and a summary that adds up", rep["items"] and sum(rep["summary"].values()) == len(rep["items"]))
class Broken(Radio):
    def bias_snapshot(self): raise RuntimeError("boom")
    @property
    def decoders(self): raise RuntimeError("no decoders")
    @decoders.setter
    def decoders(self, v): pass
rep = D.run_checks(Broken(Dev("sdrplay")), dict(S), [], run=fw(ALLOW), windows=False)
check("a check that crashes becomes an info line and the others still run", any(i["id"] == "err_check_decoders" and i["level"] == "info" for i in rep["items"]) and any(i["id"] == "rx" for i in rep["items"]))

# ---------------------------------------------------------------- the zip
folder = tempfile.mkdtemp()
os.makedirs(os.path.join(folder, "logs"))
open(os.path.join(folder, "logs", "andysdr.log"), "w").write("line\n" * 200000)           # ~1 MB: only the tail is kept
open(os.path.join(folder, "logs", "andysdr.log.1"), "w").write("older log\n")
open(os.path.join(folder, "logs", "crash.txt"), "w").write("Fatal Python error\n")
open(os.path.join(folder, "logs", "other.txt"), "w").write("not wanted\n")
settings = {"frequency": 145500000, "password": "ANDY-SECRET", "nested": {"api_token": "abc", "ok": 1}, "alerts": [{"secret": "x"}], "empty_key": ""}
z = zipfile.ZipFile(io.BytesIO(D.build_zip(rep, settings, folder)))
names = z.namelist()
check("the zip has the report, settings and logs, nothing else", {"report.txt", "report.json", "settings.json", "logs/andysdr.log", "logs/andysdr.log.1", "logs/crash.txt"} == set(names))
st = z.read("settings.json").decode()
check("the password and tokens are removed from the settings", "ANDY-SECRET" not in st and "abc" not in st and '"x"' not in st and "145500000" in st)
check("the log is cut to its tail", 300_000 < len(z.read("logs/andysdr.log")) <= 400_100)
check("the readable report starts with the version and lists the checks", z.read("report.txt").decode().splitlines()[0].startswith("AndySDR ") and "[OK  ]" in z.read("report.txt").decode())
check("the password is not in the report either", b"ANDY-SECRET" not in z.read("report.txt") + z.read("report.json"))
z2 = zipfile.ZipFile(io.BytesIO(D.build_zip(rep, {}, tempfile.mkdtemp())))
check("with no log folder it still makes a zip", "report.txt" in z2.namelist())

# ---------------------------------------------------------------- why no weather pictures
import tempfile, time as _t
class PSats:
    def __init__(s, on=(), obs=(51.5, -0.1), passes=()): s.on, s.obs, s.p = on, obs, passes
    def cfg(s): return {k: {"enabled": True} for k in s.on} | {"other": {"enabled": False}}
    def observer(s): return s.obs
    def upcoming(s, now=None): return list(s.p)
class PSch:
    def __init__(s, sats, jobs=()): s.sats, s._j = sats, jobs
    def jobs(s): return list(s._j)
class PRadio:
    def __init__(s, sats, jobs=(), folder=""):
        s.scheduler = PSch(sats, jobs)
        class De: images_dir = folder
        s.decoders = De()
        s.s = {}
now = _t.time()
empty = tempfile.mkdtemp()
r = D.check_pictures(PRadio(PSats(), folder=empty), now, empty)
check("nothing switched on says so, with where to switch it on", any(i["id"] == "pic_none" and "SCHEDULER" in i["fix"] for i in r))
check("no picture yet is only information", [i for i in r if i["id"] == "pic_last"][0]["level"] == "info")
pics = tempfile.mkdtemp(); os.makedirs(os.path.join(pics, "lrpt_1")); f = os.path.join(pics, "lrpt_1", "a.png"); open(f, "wb").write(b"x")
os.utime(f, (now - 3 * 86400, now - 3 * 86400))
P = [{"name": "Meteor-M N2-4", "aos": now + 3600, "max_el": 41}]
r = D.check_pictures(PRadio(PSats(("meteor_m24",), passes=P), folder=pics), now, pics, satdump_found=True)
d = {i["id"]: i for i in r}
check("a 3-day-old newest picture while recording is on is a warning with the age", d["pic_last"]["level"] == "warn" and "3.0 days" in d["pic_last"]["title"])
check("next pass is named with its time and elevation", "Meteor-M N2-4" in d["pic_pass"]["title"] and "41" in d["pic_pass"]["title"])
r = D.check_pictures(PRadio(PSats(("meteor_m24",), passes=[]), folder=pics), now, pics, satdump_found=True)
check("no passes at all points at min elevation / day boxes", [i for i in r if i["id"] == "pic_pass"][0]["level"] == "warn" and "Min elev" in [i for i in r if i["id"] == "pic_pass"][0]["fix"])
r = D.check_pictures(PRadio(PSats(("meteor_m24",), passes=P), folder=pics), now, pics, satdump_found=False)
check("missing SatDump is a problem that says recordings can't become pictures", [i for i in r if i["id"] == "pic_satdump"][0]["level"] == "bad")
r = D.check_pictures(PRadio(PSats(("meteor_m24",), obs=None), folder=pics), now, pics, satdump_found=True)
check("no position: the pass line is left to the position check", not any(i["id"] == "pic_pass" for i in r))
os.utime(f, (now - 3600, now - 3600))
r = D.check_pictures(PRadio(PSats(("meteor_m24",), passes=P), folder=pics), now, pics, satdump_found=True)
check("a picture from the last hour is fine", [i for i in r if i["id"] == "pic_last"][0]["level"] == "ok")
r = D.check_pictures(PRadio(PSats(), jobs=[{"action": "wefax", "enabled": True}], folder=pics), now, pics)
check("a weather-fax capture alone counts as switched on", not any(i["id"] == "pic_none" for i in r))

print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)