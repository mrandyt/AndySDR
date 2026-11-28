"""Working out what signal level was producing frames, from the frame counters and the signal trace.

Counters come from real `.cadu` files where they are to hand. The signal traces are simulated, because the traces
of those passes were never kept - which is the whole reason for the change. The simulation is the honest kind: a
pass-shaped signal, a true decoding threshold, and frames decoded or lost one at a time according to how far the
signal stood above that threshold at the moment each was sent. The test then asks whether the threshold can be
recovered from nothing but the counters and the trace.
"""
import math, os, random, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from andysdr import framesnr as F

fails = 0


def check(name, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name, "" if ok else extra)
    fails += 0 if ok else 1


def trace(dur, peak_at, peak_db=10.0, floor_db=-12.0, width=0.35):
    """A pass-shaped signal trace: rises to `peak_db` at `peak_at` and falls away again."""
    out = []
    t = 0.0
    while t <= dur:
        x = (t - peak_at) / (dur * width)
        out.append((t, round(floor_db + (peak_db - floor_db) * math.exp(-x * x), 2)))
        t += 1.0
    return out


def snr_at(tr, t):
    i = min(len(tr) - 1, max(0, int(round(t))))
    return tr[i][1]


def heard(tr, dur, thresh, sharp=1.2, lo=100000, rate=F.FRAME_RATE, seed=3):
    """The counters of the frames that got through, given a true decoding threshold in dB."""
    r = random.Random(seed)
    out = []
    n = int(dur * rate)
    for i in range(n):
        p = 1.0 / (1.0 + math.exp(-(snr_at(tr, i / rate) - thresh) * sharp))
        if r.random() < p:
            out.append(lo + i)
    return out


# --- coverage, which rests on nothing ---------------------------------------------------------------------
cs = [2509361 + i for i in range(1432) if i % 5 != 4]       # first and last both heard
c = F.coverage(cs)
check("coverage counts what was sent, not just what arrived", c["sent"] == 1432 and c["frames"] == 1146, c)
check("...as a percentage", abs(c["pct"] - 80.0) < 0.1, c["pct"])
check("a single frame is not called 0%", F.coverage([7])["pct"] == 100.0, F.coverage([7]))
check("nothing heard is nothing", F.coverage([])["frames"] == 0)

# --- the density profile ----------------------------------------------------------------------------------
d = F.density(list(range(int(300 * F.FRAME_RATE))))
check("a perfect window reads as full all through", len(d) == 300 and min(d) == 1.0, (len(d), min(d) if d else None))
n = int(300 * F.FRAME_RATE)
gap = F.density([i for i in range(n) if i < n / 4 or i > n * 3 / 4])     # heard at both ends, lost in the middle
check("...and one lost in the middle shows the hole", gap[10] == 1.0 and gap[len(gap) // 2] == 0.0
      and gap[-10] == 1.0, (gap[10], gap[len(gap) // 2], gap[-10]))

# --- recovering a threshold that is known ---------------------------------------------------------------------
# A 585 s pass peaking at 10 dB at 300 s, with frames decoding from about 4 dB upwards.
dur, peak, TRUE = 585, 300, 4.0
tr = trace(dur, peak)
cs = heard(tr, dur, TRUE)
check("the simulated pass heard a sensible share of the frames", 0.2 < len(cs) / (dur * F.FRAME_RATE) < 0.8,
      round(len(cs) / (dur * F.FRAME_RATE), 2))
d = F.density(cs)
off, ok = F.align(d, tr, dur)
check("the window is placed", ok and off is not None, (off, ok))
# the first frame heard was sent at (cs[0]-100000)/rate seconds into the recording
真 = (cs[0] - 100000) / F.FRAME_RATE
check("...and placed where the frames really started", ok and abs(off - 真) < 25, (off, round(真, 1)))

with tempfile.TemporaryDirectory() as td:
    def write_cadu(path, counters):
        with open(path, "wb") as fh:
            for c in counters:
                fh.write(F.ASM + bytes([0x00, 0x40 | F.IMAGERY_VCID]) + c.to_bytes(3, "big") + bytes(F.CADU - 9))

    p = os.path.join(td, "meteor_m2-x_lrpt.cadu")
    write_cadu(p, cs)
    check("the .cadu is found in a pass folder", F.find_cadu(td) == p, F.find_cadu(td))
    check("...and read back exactly", F.counters(p) == cs)
    r = F.measure(p, tr, dur)
    check("frames and coverage are reported", r and r["frames"] == len(cs), r)
    check("...with the rate floor this pass implies", r.get("rate_floor") and r["rate_floor"] <= F.FRAME_RATE + 0.01,
          r.get("rate_floor"))
    check("the threshold is recovered within a decibel or so", r.get("snr_lo") is not None
          and abs(r["snr_lo"] - TRUE) <= 1.5, (r.get("snr_lo"), TRUE))
    check("...and the weakest is below the typical, which is below the best",
          r["snr_lo"] <= r["snr_med"] <= r["snr_hi"], (r["snr_lo"], r["snr_med"], r["snr_hi"]))
    check("...and the frames took a sensible length of time to arrive", 100 < r["secs"] < dur, r.get("secs"))
    check("a sentence is produced", "frames sent while it was in reach" in F.sentence(r), F.sentence(r))

    # the same pass with a harder threshold must come out higher, not the same
    cs8 = heard(tr, dur, 8.0)
    p8 = os.path.join(td, "hard.cadu")
    write_cadu(p8, cs8)
    r8 = F.measure(p8, tr, dur)
    check("a receiver that needs more signal is reported as needing more",
          r8.get("snr_lo") is not None and r8["snr_lo"] > r["snr_lo"] + 1.0, (r.get("snr_lo"), r8.get("snr_lo")))
    check("...and it heard fewer frames", r8["frames"] < r["frames"], (r["frames"], r8["frames"]))

    # a pass that heard almost nothing: coverage only, no threshold invented
    p2 = os.path.join(td, "tiny.cadu")
    write_cadu(p2, [600000 + i for i in range(0, 57, 11)])
    r2 = F.measure(p2, trace(475, 240), 475)
    check("a handful of frames gives coverage but no threshold",
          r2 and r2["frames"] == 6 and r2.get("snr_lo") is None, r2)
    check("...and the sentence says only what is known", "down to" not in F.sentence(r2), F.sentence(r2))
    check("no .cadu at all is no measurement", F.measure(os.path.join(td, "nope.cadu"), tr, dur) is None)

# --- it refuses when it cannot tell ---------------------------------------------------------------------------
check("too few frames to place anything", F.align(F.density([500 + i for i in range(30)]), tr, dur)[1] is False)
flat = trace(dur, peak, peak_db=0.0, floor_db=0.0)
steady = [100000 + i for i in range(int(160 * F.FRAME_RATE))]
check("a flat signal trace cannot place a short window", not F.align(F.density(steady), flat, dur)[1])
full = [i for i in range(int(dur * F.FRAME_RATE))]
off3, ok3 = F.align(F.density(full), tr, dur)
check("a window as long as the recording needs no matching", ok3 and off3 == 0, (off3, ok3))

# --- against the real files, when they are to hand -------------------------------------------------------------
REAL = "/mnt/user-data/uploads/images"
real = {"lrpt_20261127_141519_240k_rx2": (585, 879, 1432),   # 80 deg, second receiver
        "lrpt_20261126_101147_240k": (580, 1750, 3165),      # 70 deg - the pass the frame rate was measured from
        "lrpt_20261128_092524_240k": (564, 181, 263)}        # 54 deg, main radio
seen = 0
for name, (dur_s, want_got, want_span) in real.items():
    p = os.path.join(REAL, name, "meteor_m2-x_lrpt.cadu")
    if not os.path.exists(p) or os.path.getsize(p) == 0:
        continue
    seen += 1
    cv = F.coverage(F.counters(p))
    check(f"real pass {name[5:13]}: frames counted", cv["frames"] == want_got, (cv["frames"], want_got))
    check(f"real pass {name[5:13]}: frames sent", cv["sent"] == want_span, (cv["sent"], want_span))
    check(f"real pass {name[5:13]}: implied rate is not above the stored one",
          (want_span / dur_s) <= F.FRAME_RATE + 0.01, round(want_span / dur_s, 3))
if not seen:
    print("note: no staged .cadu files to hand, real-pass checks skipped")

# --- the whole way through, as the radio does it --------------------------------------------------------------
# A PassMeter collects readings during the pass, its summary goes into the pass log, SatDump leaves a .cadu, and
# the measurement is filed against that pass and the trace thrown away.
from andysdr import passlog as PL

with tempfile.TemporaryDirectory() as td:
    tr2 = trace(400, 200)
    meter = PL.PassMeter()
    meter.t0 = 1000.0
    for t, v in tr2:
        meter.add(1000.0 + t, v, 28.0)
    sm = meter.summary()
    check("the meter keeps a signal trace", len(sm.get("snr_trace") or []) > 50, len(sm.get("snr_trace") or []))
    check("...thinned, not one point a second", len(sm["snr_trace"]) < len(tr2), (len(sm["snr_trace"]), len(tr2)))
    check("...and the old summary figures still come out", sm.get("snr_peak") is not None and sm.get("samples") > 100,
          sm.get("samples"))

    plog = PL.PassLog(os.path.join(td, "passes.jsonl"))
    plog.add({"key": "k1", "sat": 1, "name": "Meteor-M N2-4", "start": 1000, "end": 1400, **sm})
    out = os.path.join(td, "pass1")
    os.makedirs(out)
    cs2 = heard(tr2, 400, 3.0, lo=777000)
    with open(os.path.join(out, "meteor_m2-x_lrpt.cadu"), "wb") as fh:
        for c in cs2:
            fh.write(F.ASM + bytes([0x00, 0x40 | F.IMAGERY_VCID]) + c.to_bytes(3, "big") + bytes(F.CADU - 9))
    m = F.for_pass(plog, "k1", out)
    rec = plog.find("k1")
    check("the measurement is filed against the pass", rec.get("rx_frames", {}).get("frames") == len(cs2),
          rec.get("rx_frames"))
    check("...the threshold with it", rec["rx_frames"].get("snr_lo") is not None, rec.get("rx_frames"))
    check("...and the trace is thrown away once used", "snr_trace" not in rec, list(rec)[-4:])
    check("the view hands the frames to the screen", (plog.view(5)[0] or {}).get("rx_frames") is not None)

    # a pass with no .cadu still loses its trace rather than keeping a few kilobytes for ever
    plog.add({"key": "k2", "name": "nothing heard", "start": 1, "end": 2, **sm})
    F.for_pass(plog, "k2", os.path.join(td, "nope"))
    check("a pass that heard nothing keeps no trace either", "snr_trace" not in plog.find("k2"))
    check("...and claims no frames", plog.find("k2").get("rx_frames") is None, plog.find("k2").get("rx_frames"))

# --- filling in the passes recorded before any of this existed -------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    import time as _t
    imgs = os.path.join(td, "images")
    os.makedirs(imgs)
    plog = PL.PassLog(os.path.join(td, "p.jsonl"))
    t0 = _t.mktime(_t.strptime("20261127_141519", "%Y%m%d_%H%M%S"))
    cs3 = [900000 + i for i in range(0, 1000, 2)]

    def folder(name, counters):
        d = os.path.join(imgs, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "meteor_m2-x_lrpt.cadu"), "wb") as fh:
            for c in counters:
                fh.write(F.ASM + bytes([0x00, 0x40 | F.IMAGERY_VCID]) + c.to_bytes(3, "big") + bytes(F.CADU - 9))
        return d

    folder("lrpt_20261127_141519_240k_rx2", cs3)
    folder("lrpt_20261127_141519_240k", cs3[:100])              # the main radio's copy of the same pass
    plog.add({"key": "old1", "name": "Meteor-M N2-4", "start": int(t0), "end": int(t0) + 585, "rx": "rx2"})
    plog.add({"key": "old2", "name": "Meteor-M N2-4", "start": int(t0), "end": int(t0) + 585})
    plog.add({"key": "old3", "name": "nothing on disk", "start": int(t0) - 99999, "end": int(t0) - 99000})
    n = F.backfill(plog, imgs)
    check("earlier passes are filled in from the .cadu files on disk", n == 2, n)
    check("...the second receiver matched to its own folder",
          plog.find("old1")["rx_frames"]["frames"] == len(cs3), plog.find("old1").get("rx_frames"))
    check("...and the main radio to its own, not the other one",
          plog.find("old2")["rx_frames"]["frames"] == 100, plog.find("old2").get("rx_frames"))
    check("...with coverage, which is exact", abs(plog.find("old1")["rx_frames"]["pct"] - 50.1) < 0.05,
          plog.find("old1")["rx_frames"])
    check("...and no threshold invented for a pass whose trace was never kept",
          "snr_lo" not in plog.find("old1")["rx_frames"])
    check("a pass with no folder is left alone", plog.find("old3").get("rx_frames") is None)
    check("...but is not looked at twice", plog.find("old3").get("frames_checked") is True)
    check("running it again does nothing", F.backfill(plog, imgs) == 0)
    check("a missing images folder is not an error", F.backfill(plog, os.path.join(td, "gone")) == 0)

print("FAILED " + str(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
