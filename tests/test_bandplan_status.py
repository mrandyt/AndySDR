"""Band plan status: each entry says what AndySDR can do with it (decoded, needs antenna, listen, detect only, encrypted)."""
import json, os, shutil, subprocess, sys
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
WEB = os.path.join(ROOT, "andysdr", "web")
fails = []
def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name, "" if ok else extra)
    if not ok: fails.append(name)

node = shutil.which("node")
if not node:
    print("SKIP node not installed"); sys.exit(0)

js = r"""
const fs = require("fs"), vm = require("vm");
const ctx = {};
vm.createContext(ctx);
const obs = process.argv[2] === "none" ? "null" : "[" + process.argv[2] + "]";
vm.runInContext(fs.readFileSync(process.argv[1], "utf8") + "\n;BP_OBSERVER = " + obs + ";\n;globalThis.__o = {BANDPLAN, bpStatus, BP_STATUS};", ctx);
const { BANDPLAN, bpStatus, BP_STATUS } = ctx.__o;
const names = process.argv.slice(3);
const out = {};
for (const n of names) { const b = BANDPLAN.find((x) => x.name.startsWith(n)); out[n] = b ? bpStatus(b).key : null; }
const counts = {}; for (const b of BANDPLAN) { const k = bpStatus(b).key; counts[k] = (counts[k] || 0) + 1; }
const missing = BANDPLAN.filter((b) => !BP_STATUS[bpStatus(b).key]).length;
out.__counts = counts; out.__missing = missing;
console.log(JSON.stringify(out));
"""
expect = {
    "ACARS 131.525": "decoded",
    "Airwave TETRA (downlink)": "encrypted",
    "TETRA – defence": "detect",
    "Met Office WEFAX 8040": "hardware",
    "Meteor-M LRPT 137.900": "decoded",
    "Weather satellites (HRPT)": "hardware",
    "ADS-B / Mode S": "decoded",
    "UK pager channels": "decoded",
    "AIS 1": "detect",
    "433 MHz ISM": "detect",
    "20 m amateur": "voice",
    "Shannon VOLMET 5505": "voice",
    "GSM-R railway (uplink)": "detect",
    "PMR446 walkie-talkies": "voice",
    "VDL Mode 2": "detect",
    "NAVTEX 490": "detect",
    "Met aids": "decoded",
    "GOES HRIT 1694.1": "low",
    "Elektro-L LRIT": "low",
    "Inmarsat": "detect",
}
r = subprocess.run([node, "-e", js, os.path.join(WEB, "bandplan.js"), "52.668,-2.472"] + list(expect), capture_output=True, text=True)
r_none = subprocess.run([node, "-e", js, os.path.join(WEB, "bandplan.js"), "none", "GOES HRIT"], capture_output=True, text=True)
check("no receiver position: GEO satellite not marked too low", json.loads(r_none.stdout)["GOES HRIT"] == "hardware", r_none.stdout + r_none.stderr[-200:])
check("node loads bandplan.js", r.returncode == 0, r.stderr[-400:])
got = json.loads(r.stdout) if r.returncode == 0 else {}
for name, want in expect.items():
    check(f"{name} -> {want}", got.get(name) == want, f"got {got.get(name)}")
check("every entry gets a known status", got.get("__missing") == 0, got.get("__missing"))

html = open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
check("band plan has a Status column", "<th>Status</th>" in html)
check("band plan has a Status filter", 'id="bpStat"' in html)
app = open(os.path.join(WEB, "app.js"), encoding="utf-8").read()
check("table renders status and filters on it", "bpStatus(b)" in app and 'stat && bpStatus(b).key' in app)

print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
print("counts:", got.get("__counts"))
sys.exit(1 if fails else 0)
