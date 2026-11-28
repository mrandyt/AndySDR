"""Find the best RF gain for the antenna and band you are actually on.

There is no single right gain. Too little and the receiver's own noise drowns what the antenna hears; too much and
nothing improves while strong signals start to distort. The right setting is the lowest gain at which the antenna's
own noise, not the receiver's, is what you are listening to - and that depends on the antenna, the band and what else
is on the air, so it has to be measured rather than guessed.

The measurement is simple. Turn the gain up step by step and watch the noise floor:

* While the receiver's own noise is the bigger of the two, turning the gain up barely moves the floor.
* Once the antenna's noise is the bigger, the floor follows the gain one dB for one dB.

The bend between the two is the answer. This module does the arithmetic on a set of (gain, noise floor) readings;
`andysdr.engine` does the sweeping. The gain chosen is the lowest one where the floor is following the gain at least
`WANT_SLOPE` as fast, which is the same as saying the antenna's noise is about 9 dB above the receiver's own and the
receiver is costing you under half a dB. Above that point more gain buys nothing and only loses headroom.
"""
import logging

log = logging.getLogger("andysdr.gaincal")

WANT_SLOPE = 0.90          # the floor must follow the gain this closely: ~0.5 dB from the best this antenna can give
WINDOW_DB = 7.0            # the slope is measured over a window this wide, so one noisy reading cannot decide it
MIN_POINTS = 5


def plan(lo, hi, steps=None, most=22):
    """The gains to try, lowest first. `steps` is the receiver's own list of gains when it has one."""
    lo, hi = float(lo), float(hi)
    if steps:
        g = sorted({round(float(s), 1) for s in steps if lo - 0.01 <= float(s) <= hi + 0.01})
        if len(g) > most:                               # thin an over-long list out evenly, always keeping both ends
            keep = [g[round(i * (len(g) - 1) / (most - 1))] for i in range(most)]
            g = sorted(set(keep))
        if len(g) >= MIN_POINTS:
            return g
    n = max(MIN_POINTS, min(most, int(round((hi - lo) / 2.0)) + 1))
    return [round(lo + (hi - lo) * i / (n - 1), 1) for i in range(n)]


def _slope_at(points, i):
    """How fast the noise floor is following the gain around point `i`, in dB per dB (least squares over a window)."""
    g0 = points[i][0]
    win = [p for p in points if abs(p[0] - g0) <= WINDOW_DB]
    if len(win) < 3:
        win = points[max(0, i - 1):i + 2]
    if len(win) < 2:
        return 0.0
    n = len(win)
    mx = sum(p[0] for p in win) / n
    my = sum(p[1] for p in win) / n
    sxx = sum((p[0] - mx) ** 2 for p in win)
    if sxx <= 1e-9:
        return 0.0
    sxy = sum((p[0] - mx) * (p[1] - my) for p in win)
    return sxy / sxx


def choose(points, overload_from=None, want=WANT_SLOPE, keep=None):
    """Pick the gain from a sweep.

    `points` is [(gain dB, noise floor dBFS)], any order. `overload_from` is the lowest gain at which the receiver
    reported it was being overloaded, if it did. `keep` is the gain in use, kept when the measurement turns out to
    have nothing to say about which gain is better.

    Returns {"gain", "why", "slope", "headroom", "points"} - `why` is a sentence for the screen.
    """
    pts = sorted((float(g), float(f)) for g, f in points if f is not None)
    if len(pts) < MIN_POINTS:
        return None
    lo, hi = pts[0][0], pts[-1][0]
    top = hi if overload_from is None else max(lo, min(hi, float(overload_from) - 2.0))
    slopes = [(g, _slope_at(pts, i)) for i, (g, _f) in enumerate(pts)]
    rise = pts[-1][1] - pts[0][1]

    pick = why = None
    usable = [(g, sl) for g, sl in slopes if g <= top + 1e-9] or slopes[:1]
    capped = overload_from is not None and len(usable) < len(slopes)
    if rise < 3.0:
        # The floor hardly moved across the whole range.  Either the aerial is quieter than the receiver even at full
        # gain, or nothing is reaching the receiver at all - the two look identical from here, so say both.  Maximum
        # gain is right either way.
        pick = usable[-1][0]
        why = ("The noise floor barely moved across the whole gain range (%.1f dB). Either this aerial is very quiet or "
               "nothing is reaching the receiver - worth checking the aerial and the lead. Using the most gain available "
               "meanwhile." % rise)
    elif usable[0][1] >= want:
        # The floor is already following the gain one-for-one at the lowest setting the receiver has, so the bend is
        # below its range: the aerial - or a preamplifier in front of it - is louder than the receiver everywhere.
        # Nothing here says one gain is better than another, so changing it would be guessing.  Leave it alone.
        pick = usable[0][0] if keep is None else max(usable[0][0], min(top, float(keep)))
        why = ("The noise floor follows the gain even at the lowest setting, so the aerial - or a preamplifier in front "
               "of it - is already louder than the receiver across the whole range. Gain is not what is limiting you "
               "here and no setting is better than another for sensitivity, so it has been left where it was. What is "
               "worth watching at this aerial is overload at the top end, not sensitivity at the bottom.")
    else:
        for g, sl in usable:
            if sl >= want:
                pick = g
                why = ("The noise floor follows the gain from %.0f dB upwards, so from there on you are hearing the "
                       "aerial rather than the receiver. More gain than this buys nothing." % g)
                break
        if pick is None:
            pick = usable[-1][0]
            why = (("The receiver was being overloaded before the aerial's own noise took over, so the gain is held at "
                    "%.0f dB - as much as it can take here." % pick) if capped else
                   ("The noise floor was still rising more slowly than the gain at the top of the range, so this aerial "
                    "is quieter than the receiver: use all the gain there is."))
    if capped and "overload" not in why:
        why += " Held below %.0f dB, where the receiver reported it was being overloaded." % float(overload_from)
    at = dict(slopes).get(pick, 0.0)
    return {"gain": round(pick, 1), "why": why, "slope": round(at, 2),
            "headroom": round(hi - pick, 1), "points": [[round(g, 1), round(f, 1)] for g, f in pts]}
