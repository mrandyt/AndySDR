"""Auto-identify changing the demodulator, and knowing when not to.

Taken from a real fault. On 131.825 MHz - ACARS, which is AM - auto-identify alternated between "AM voice" and
"FM voice" about once a second and switched the demodulator each time:

    16:00:30  AM voice   snr 46.3 dB
    16:00:32  FM voice   snr 18.3 dB
    16:00:37  AM voice   snr 43.6 dB
    16:00:38  FM voice   snr 19.2 dB

ACARS is silence most of the time. The classifier went on reading between the bursts, where there is nothing to
read, and noise is as likely to look like FM as AM. Demodulating an AM signal as FM sounds exactly like bad
distortion, which is how it was noticed.
"""
import os, sys, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from andysdr import engine as E

fails = 0


def check(name, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name, "" if ok else extra)
    fails += 0 if ok else 1


class Rx:
    def __init__(self):
        self.modes = []

    def set_mode(self, m):
        self.modes.append(m)


def radio(mode="AM", squelch=-69.0):
    r = types.SimpleNamespace(mode=mode, rx=Rx(), sq_open=True, events=[],
                              s={"squelch": squelch}, auto_mode_lock=False, auto_mode_vote=(None, 0),
                              auto_mode_t=0.0, auto_mode_swaps=0)
    r._emit = lambda k, v: r.events.append((k, v))
    r._push_state = lambda *a, **k: None
    r._auto_on_signal = lambda res: E.Radio._auto_on_signal(r, res)
    return r


def feed(r, kind, mode, snr, open_sq=True, n=1):
    for _ in range(n):
        r.sq_open = open_sq
        E.Radio._auto_voice_mode(r, {"kind": kind, "mode": mode, "snr": snr, "label": kind})


# --- the real fault: it must not flip to FM between the bursts ---------------------------------------------
r = radio(mode="AM")
for _ in range(12):                                   # a burst, then the gap, over and over, as ACARS really is
    feed(r, "AM voice", "AM", 46.0, open_sq=True, n=2)
    feed(r, "FM voice", "NFM", 18.0, open_sq=False, n=2)
check("an AM burst signal is never switched to FM between bursts", r.rx.modes == [], r.rx.modes)
check("...and the mode is left where it was", r.mode == "AM", r.mode)

# --- but a signal that really is FM still gets the demodulator it needs --------------------------------------
r2 = radio(mode="AM")
feed(r2, "FM voice", "NFM", 25.0, open_sq=True, n=E.AUTO_MODE_VOTES)
check("a steady FM signal does switch the demodulator", r2.rx.modes == ["NFM"], r2.rx.modes)
check("...and it took a run of readings, not two", E.AUTO_MODE_VOTES >= 4, E.AUTO_MODE_VOTES)
check("...and it was said on screen", any("switched to NFM" in (v.get("text") or "") for _k, v in r2.events),
      r2.events)

r3 = radio(mode="AM")
feed(r3, "FM voice", "NFM", 25.0, open_sq=True, n=E.AUTO_MODE_VOTES - 1)
check("a run one short of the mark does not switch", r3.rx.modes == [], r3.rx.modes)

# --- readings taken off signal carry no vote either way ---------------------------------------------------------
r4 = radio(mode="AM")
feed(r4, "FM voice", "NFM", 3.0, open_sq=False, n=30)
check("thirty readings of silence change nothing", r4.rx.modes == [], r4.rx.modes)
check("...and they did not even build a vote", r4.auto_mode_vote == (None, 0), r4.auto_mode_vote)

# with the squelch wide open there is no squelch to go by, so the signal-to-noise figure decides
r5 = radio(mode="AM", squelch=-119.0)
feed(r5, "FM voice", "NFM", 2.0, open_sq=False, n=20)
check("with no squelch set, a weak reading is still treated as the gap", r5.rx.modes == [], r5.rx.modes)
feed(r5, "FM voice", "NFM", 30.0, open_sq=False, n=E.AUTO_MODE_VOTES)
check("...and a strong one is still treated as a signal", r5.rx.modes == ["NFM"], r5.rx.modes)

# --- one change at a time -----------------------------------------------------------------------------------------
r6 = radio(mode="AM")
feed(r6, "FM voice", "NFM", 25.0, open_sq=True, n=E.AUTO_MODE_VOTES)
check("the first change happens", r6.rx.modes == ["NFM"], r6.rx.modes)
r6.mode = "NFM"
feed(r6, "AM voice", "AM", 25.0, open_sq=True, n=E.AUTO_MODE_VOTES * 3)
check("a second change is held off until the first has been heard", r6.rx.modes == ["NFM"], r6.rx.modes)
r6.auto_mode_t -= E.AUTO_MODE_HOLD_S + 1
feed(r6, "AM voice", "AM", 25.0, open_sq=True, n=E.AUTO_MODE_VOTES)
check("...and allowed once the quiet period is over", r6.rx.modes == ["NFM", "AM"], r6.rx.modes)

# --- a signal it cannot make its mind up about: stop rather than keep swapping ---------------------------------------
r7 = radio(mode="AM")
for i in range(12):
    want = ("NFM", "AM")[i % 2]
    kind = ("FM voice", "AM voice")[i % 2]
    r7.auto_mode_t = 0.0                                # pretend enough time passed each round
    feed(r7, kind, want, 30.0, open_sq=True, n=E.AUTO_MODE_VOTES)
    r7.mode = want
check("a signal that keeps changing its mind is given up on", len(r7.rx.modes) <= E.AUTO_MODE_MAX_SWAPS,
      r7.rx.modes)
check("...and it says so rather than going quiet",
      any("until you retune" in (v.get("text") or "") for _k, v in r7.events), r7.events[-2:])

# --- the things it must still never do ----------------------------------------------------------------------------
r8 = radio(mode="AM")
r8.auto_mode_lock = True
feed(r8, "FM voice", "NFM", 30.0, open_sq=True, n=20)
check("a mode chosen by hand is never overridden", r8.rx.modes == [], r8.rx.modes)

for leave in ("WFM", "CW"):
    r9 = radio(mode=leave)
    feed(r9, "FM voice", "NFM", 30.0, open_sq=True, n=20)
    check(f"{leave} is left alone", r9.rx.modes == [], r9.rx.modes)

r10 = radio(mode="FM")
feed(r10, "FM voice", "NFM", 30.0, open_sq=True, n=20)
check("FM is not swapped for NFM, which is the same thing", r10.rx.modes == [], r10.rx.modes)

print("FAILED " + str(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
