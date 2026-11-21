"""Bias-T watcher.

No SDR reports the current it supplies, so this watches what the bias-T is *for*: the noise floor the
receiver sees. A powered LNA / active antenna lifts the floor; if the power is lost (short, wrong port,
cable fault) the floor falls. The floor is re-baselined whenever something that changes it is changed
(frequency, gain, antenna, sample rate, bias-T on/off), then a sustained fall is flagged.
"""
import collections
import time

DROP_DB = 6.0          # floor this far below its baseline for HOLD_S seconds = "lost power?"
HOLD_S = 4.0
SETTLE_S = 6.0         # after a change, wait this long before taking the baseline
TAU_S = 2.0            # smoothing time constant of the floor reading
HIST = 120             # seconds of history kept for the sparkline


class BiasMonitor:
    def __init__(self):
        self.key = None
        self.floor = None
        self.t_last = None
        self.t_key = 0.0
        self.baseline = None
        self.low_since = None
        self.hist = collections.deque(maxlen=HIST)
        self.t_hist = 0.0
        self.testing = False
        self.result = None
        self.overload_log = collections.deque(maxlen=600)

    def update(self, floor_db, key, now=None, overloads=0):
        now = time.time() if now is None else now
        if key != self.key:
            self.key, self.t_key, self.baseline, self.low_since = key, now, None, None
            self.floor = floor_db
        else:
            dt = max(0.0, now - (self.t_last or now))
            a = 1.0 - 2.718281828 ** (-dt / TAU_S)
            self.floor = floor_db if self.floor is None else self.floor + a * (floor_db - self.floor)
        self.t_last = now
        if self.baseline is None and now - self.t_key >= SETTLE_S:
            self.baseline = self.floor
        if self.baseline is not None and self.floor < self.baseline - DROP_DB:
            self.low_since = self.low_since or now
        else:
            self.low_since = None
        if now - self.t_hist >= 1.0:
            self.t_hist = now
            self.hist.append(round(self.floor, 1))
            self.overload_log.append((now, overloads))

    def recent(self, seconds, now=None):
        """Mean smoothed floor over the last `seconds` of history (None if not enough yet)."""
        pts = list(self.hist)[-max(1, int(seconds)):]
        return sum(pts) / len(pts) if pts else None

    def state(self, bias_on, effective, now=None):
        now = time.time() if now is None else now
        if bias_on is None:
            return "unsupported"
        if self.testing:
            return "testing"
        if not bias_on:
            return "off"
        if not effective:
            return "no_path"
        if self.baseline is None:
            return "settling"
        if self.low_since and now - self.low_since >= HOLD_S:
            return "drop"
        return "ok"

    def overload_rate(self, now=None):
        """ADC overload events per minute over the last minute."""
        now = time.time() if now is None else now
        pts = [(t, n) for t, n in self.overload_log if now - t <= 60]
        if len(pts) < 2:
            return 0.0
        return max(0, pts[-1][1] - pts[0][1]) * 60.0 / max(1.0, pts[-1][0] - pts[0][0])

    def snapshot(self, bias_on, effective, antenna, overloads, now=None):
        st = self.state(bias_on, effective, now)
        return {"supported": bias_on is not None, "on": bool(bias_on), "effective": bool(effective), "antenna": antenna,
                "state": st, "floor": None if self.floor is None else round(self.floor, 1),
                "baseline": None if self.baseline is None else round(self.baseline, 1),
                "delta": None if (self.floor is None or self.baseline is None) else round(self.floor - self.baseline, 1),
                "hist": list(self.hist), "overloads": overloads, "overload_rate": round(self.overload_rate(now), 1),
                "result": self.result}


def judge(on_db, off_db):
    """Plain-English verdict of the bias-T on/off comparison. Returns (verdict_key, text)."""
    d = on_db - off_db
    if d >= 3.0:
        return "ok", f"Working: with power on the noise floor is {d:+.1f} dB higher, so the antenna/LNA is being powered and is adding gain."
    if d <= -3.0:
        return "worse", f"Floor is {abs(d):.1f} dB LOWER with power on - unusual. Check for a short, a wrong/faulty LNA or a DC-blocked filter in line."
    return "none", (f"No change ({d:+.1f} dB) between power on and off. Either no powered LNA/antenna is connected, it is not being "
                    "powered (use Antenna B on an RSP2), or a DC block is in the way.")
