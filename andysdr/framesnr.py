"""What signal level actually produced frames, measured rather than guessed.

A pass report says the signal peaked at so many dB and ran at so many typically. Neither answers the question that
decides whether a pass is worth recording: **how strong does it have to be before frames start arriving?** The peak
is reached at the top of the pass whether or not anything was decoded, and the median is dragged down by the long
quiet minutes at each end.

The answer is already in two files that are kept anyway and never put side by side:

* the per-second signal trace taken during the pass, and
* the `.cadu` file SatDump writes, which holds every frame that was successfully decoded.

Every Meteor LRPT frame carries a virtual-channel counter that increases by one for each frame the satellite sends,
whether or not it was heard. So the counters are a clock. Two things follow:

**Coverage, which needs no assumptions at all.** Of all the frames the satellite sent between the first one heard
and the last, the fraction actually received. 879 frames spanning counters 2509361 to 2510792 means 1432 were sent
and 61% arrived. That is exact, and it is a far better measure of how a pass went than a count of pictures.

**When the frames arrived, which needs one number.** Counters convert to seconds through the frame rate, so the
run of counters becomes a window of time, and that window can be laid over the signal trace. The signal level
during it is the level that was actually producing frames, and its lower edge is the threshold.

`FRAME_RATE` is that one number, and it was measured, not looked up: on a 70 degree pass on 26 Nov 2026 the
counters spanned 3165 frames across a 580 second recording that decoded from the first second to the last, which
puts the rate at 5.457 frames a second. It cannot be lower than that - the satellite would have had to send those
frames in less time than the recording lasted. Any later pass that implies a higher rate is a sign the figure
needs raising, and one is logged loudly when it turns up; it is not applied on its own, because a recording whose
start or end time was mis-filed would imply an impossible rate and quietly spoil every reading after it.

Nothing here is reported unless the window can be placed with confidence. A pass whose frames all arrived in a
ten-second burst gives no shape to match against the signal trace, and in that case the coverage figure is
reported and the threshold is not.
"""
import logging
import math
import os
import time

log = logging.getLogger("andysdr.framesnr")

ASM = bytes.fromhex("1acffc1d")     # the marker every CADU frame starts with
CADU = 1024                         # bytes per frame, Meteor LRPT
IMAGERY_VCID = 5                    # the virtual channel carrying MSU-MR; the only one SatDump keeps

FRAME_RATE = 5.46                   # VCID-5 frames per second - measured, see the docstring
MIN_FRAMES = 40                     # fewer than this and there is not enough to place a window with
MIN_WINDOW_S = 20.0                 # ...nor is a window shorter than this worth laying over a per-second trace
MIN_SHAPE = 0.04                    # the frame density must vary at least this much to carry any shape
MIN_CORR = 0.45                     # ...and must follow the signal at least this closely to be believed
MIN_LEAD = 1.5                      # the best position must beat the best one well away from it by this much


def counters(path):
    """The imagery-channel frame counters in a .cadu file, in the order they were sent. [] if there are none."""
    if not path or not os.path.exists(path):
        return []
    try:
        if os.path.getsize(path) < CADU:
            return []
        with open(path, "rb") as fh:
            blob = fh.read()
    except OSError as e:
        log.warning("cannot read %s: %s", path, e)
        return []
    out = []
    for i in range(0, len(blob) - CADU + 1, CADU):
        if blob[i:i + 4] == ASM and (blob[i + 5] & 0x3F) == IMAGERY_VCID:
            out.append(int.from_bytes(blob[i + 6:i + 9], "big"))
    return sorted(set(out))


def find_cadu(out_dir):
    """The .cadu SatDump left in a pass folder, or None."""
    try:
        for name in sorted(os.listdir(out_dir)):
            if name.endswith(".cadu"):
                p = os.path.join(out_dir, name)
                if os.path.getsize(p) >= CADU:
                    return p
    except OSError:
        pass
    return None


def coverage(cs):
    """{"frames", "sent", "pct"} - how many of the frames the satellite sent between the first and last heard arrived."""
    if len(cs) < 2:
        return {"frames": len(cs), "sent": len(cs), "pct": 100.0 if cs else 0.0}
    sent = cs[-1] - cs[0] + 1
    return {"frames": len(cs), "sent": sent, "pct": round(100.0 * len(cs) / sent, 1)}


def density(cs, rate=FRAME_RATE):
    """Per second of the reception window, the fraction of the frames sent in that second which arrived."""
    if len(cs) < 2 or rate <= 0:
        return []
    lo, span = cs[0], cs[-1] - cs[0] + 1
    secs = max(1, int(round(span / rate)))
    got = [0] * secs
    for c in cs:
        got[min(secs - 1, int((c - lo) / rate))] += 1
    # How many frames the satellite sent in each of those seconds, counted exactly rather than averaged: the rate
    # is not a whole number, so a second holds five frames or six, and averaging makes a perfect window look ragged.
    out = []
    for i, g in enumerate(got):
        sent = math.ceil((i + 1) * rate) - math.ceil(i * rate)
        if i == secs - 1:
            sent = max(1, span - math.ceil(i * rate))
        out.append(min(1.0, g / max(1, sent)))
    return out


def _shape(d):
    """How much the density varies. A flat profile - all ones, or all much the same - can match anywhere."""
    if len(d) < 3:
        return 0.0
    m = sum(d) / len(d)
    return (sum((x - m) ** 2 for x in d) / len(d)) ** 0.5


def _series(trace, dur):
    """The signal level for every second of the recording, filled in between the readings that were kept.

    The trace is thinned when it is stored - a reading every few seconds is plenty for this - so the seconds in
    between have to be filled in or two thirds of the frame window would have nothing to match against.
    """
    pts = sorted((float(t), float(v)) for t, v in trace if t is not None and v is not None)
    if len(pts) < 2:
        return {}
    out, j = {}, 0
    for sec in range(0, int(dur) + 1):
        while j + 2 < len(pts) and pts[j + 1][0] <= sec:
            j += 1
        (t0, v0), (t1, v1) = pts[j], pts[j + 1]
        if sec < t0 - 6 or sec > pts[-1][0] + 6:
            continue                                    # outside the readings altogether: do not invent a level
        if t1 <= t0:
            out[sec] = v0
        else:
            f = min(1.0, max(0.0, (sec - t0) / (t1 - t0)))
            out[sec] = v0 + (v1 - v0) * f
    return out


def _corr(a, b):
    """How closely two series rise and fall together, -1 to 1. Zero when either never changes."""
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    va = math.sqrt(sum((x - ma) ** 2 for x in a))
    vb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if va < 1e-9 or vb < 1e-9:
        return 0.0
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (va * vb)


def align(d, trace, dur):
    """Where in the recording the reception window sits.

    `trace` is [(seconds from the start of the recording, SNR dB)]. The frame density and the signal level rise and
    fall together, so the window goes where the two track each other most closely. Returns (offset seconds,
    confident), and the offset is None when the density has no shape to match, when it does not follow the signal,
    or when a position well away from the winner fits nearly as well.
    """
    n = len(d)
    if n < MIN_WINDOW_S or not trace or dur <= 0 or n > dur + 2:
        return None, False
    if _shape(d) < MIN_SHAPE:
        # Frames arrived steadily throughout: there is nothing to line up. If the window is as long as the
        # recording there is only one place it can go, which is answer enough; otherwise say so.
        return (0, True) if n >= dur - 2 else (None, False)
    at = _series(trace, dur)
    if len(at) < 3:
        return None, False
    scores = []
    for off in range(0, int(dur) - n + 2):
        xs, ys = [], []
        for i, v in enumerate(d):
            s = at.get(off + i)
            if s is not None:
                xs.append(v)
                ys.append(s)
        if len(xs) >= n * 0.6:
            scores.append((_corr(xs, ys), off))
    if not scores:
        return None, False
    scores.sort(reverse=True)
    best, off = scores[0]
    if best < MIN_CORR:
        return None, False                              # the frames did not come and go with the signal at all
    far = [sc for sc, o in scores if abs(o - off) > n / 3.0]    # the best position well away from the winner
    if far and best < far[0] * MIN_LEAD:
        return None, False                              # two places fit about as well: do not pick one and pretend
    return off, True


def measure(cadu_path, trace, dur, rate=FRAME_RATE):
    """What the signal was doing while frames were arriving.

    Returns None if there is no usable .cadu. Otherwise {"frames", "sent", "pct", "rate_floor"} always, and
    {"secs", "snr_lo", "snr_med", "snr_hi", "el_lo"} as well when the window could be placed.
    """
    cs = counters(cadu_path)
    if not cs:
        return None
    out = coverage(cs)
    span = cs[-1] - cs[0] + 1
    out["rate_floor"] = round(span / dur, 3) if dur > 0 else None   # the rate cannot be below this: see FRAME_RATE
    if len(cs) < MIN_FRAMES or span / rate < MIN_WINDOW_S:
        return out
    d = density(cs, rate)
    off, ok = align(d, trace, dur)
    if not ok or off is None:
        return out
    vals = []
    at = _series(trace, dur)
    for i, v in enumerate(d):
        s = at.get(off + i)
        if s is not None and v > 0:                     # only seconds that really produced frames
            vals.append(s)
    if len(vals) < 5:
        return out
    vals.sort()
    out["secs"] = len(vals)
    out["snr_lo"] = round(vals[max(0, int(len(vals) * 0.05))], 1)   # the weakest signal that still gave frames
    out["snr_med"] = round(vals[len(vals) // 2], 1)
    out["snr_hi"] = round(vals[-1], 1)
    out["at"] = off
    return out


def sentence(m):
    """One line for the pass report, or "" when there is nothing worth saying."""
    if not m or not m.get("frames"):
        return ""
    s = f"{m['frames']} of the {m['sent']} frames sent while it was in reach ({m['pct']:.0f}%)"
    if m.get("snr_lo") is not None:
        s += f"; frames were arriving for {m['secs']} s, down to {m['snr_lo']:.1f} dB"
    return s


def for_pass(plog, key, out_dir):
    """Measure a finished pass and file the result. Returns what was measured, or None."""
    rec = plog.find(key)
    if rec is None:
        return None
    cadu = find_cadu(out_dir)
    if not cadu:
        plog.set_frames(key, None)                      # no frames, but the trace has still served its purpose
        return None
    trace = [(float(t), float(v)) for t, v in (rec.get("snr_trace") or [])]
    dur = float(rec.get("end") or 0) - float(rec.get("start") or 0)
    if dur <= 0:
        dur = (trace[-1][0] if trace else 0.0) + 1.0
    m = measure(cadu, trace, dur)
    if m and m.get("rate_floor") and m["rate_floor"] > FRAME_RATE + 0.05:
        log.warning("this pass implies at least %.3f LRPT frames/s, above the %.2f this works from - if that "
                    "repeats, FRAME_RATE is too low (or the pass's start and end times are wrong)",
                    m["rate_floor"], FRAME_RATE)
    plog.set_frames(key, m)
    if m:
        log.info("frames: %s", sentence(m))
    return m


# --------------------------------------------------------------------------- what the archive already holds
def _folder_time(name):
    """The local time in a pass folder's name (lrpt_20261127_141519_240k_rx2), as a struct_time, or None."""
    parts = name.split("_")
    for i in range(len(parts) - 1):
        if len(parts[i]) == 8 and len(parts[i + 1]) == 6 and parts[i].isdigit() and parts[i + 1].isdigit():
            try:
                return time.strptime(parts[i] + parts[i + 1], "%Y%m%d%H%M%S")
            except ValueError:
                return None
    return None


def backfill(plog, images_dir, limit=80, within=120):
    """Work out the frame coverage of passes recorded before this was measured at all.

    The `.cadu` files have been sitting in the images folder all along, so how much of each pass actually arrived
    can be recovered from them at any time. The signal level that produced those frames cannot - no trace was kept
    for them - so these passes get coverage only, and the threshold starts appearing on passes from here on.

    Matches a pass to its folder by the time in the folder's name and which receiver recorded it. Returns how many
    passes were filled in.
    """
    try:
        names = sorted(os.listdir(images_dir), reverse=True)
    except OSError:
        return 0
    todo = [r for r in reversed(plog.rows) if r.get("start") and not r.get("rx_frames")
            and not r.get("frames_checked")][:limit]
    if not todo:
        return 0
    stamps = []
    for nm in names:
        t = _folder_time(nm)
        if t is not None:
            stamps.append((time.mktime(t), nm))
    done = 0
    for r in todo:
        want_rx2 = r.get("rx") == "rx2"
        best = None
        for ts, nm in stamps:
            if abs(ts - float(r["start"])) <= within and nm.endswith("_rx2") == want_rx2 and not nm.endswith("_merged"):
                if best is None or abs(ts - float(r["start"])) < abs(best[0] - float(r["start"])):
                    best = (ts, nm)
        r["frames_checked"] = True                       # looked at once, whatever came of it: do not look again
        if best is None:
            continue
        cs = counters(find_cadu(os.path.join(images_dir, best[1])))
        if cs:
            cv = coverage(cs)
            r["rx_frames"] = {"frames": cv["frames"], "sent": cv["sent"], "pct": cv["pct"]}
            done += 1
    plog._save()
    if done:
        log.info("frame coverage worked out for %d earlier passes from the .cadu files already on disk", done)
    return done
