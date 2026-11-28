"""Spot pictures that contain nothing (all one colour) so they can be thrown away instead of saved."""
import os

try:
    from PIL import Image
    import numpy as np
except Exception:          # Pillow missing: never claim a picture is blank
    Image = None


def is_blank(path):
    """True if the picture is one flat colour (or very nearly): no signal was decoded."""
    if Image is None:
        return False
    try:
        with Image.open(path) as im:
            im = im.convert("L")
            w, h = im.size
            if w * h == 0:
                return True
            k = max(1, max(w, h) // 600)
            if k > 1:
                im = im.reduce(k)
            a = np.asarray(im, dtype=np.float32)
    except Exception:
        return False
    if float(a.std()) < 1.5:
        return True
    vals, counts = np.unique(a.astype(np.uint8), return_counts=True)
    return counts.max() / a.size > 0.998


def discard_if_blank(path):
    """Delete the file if blank; returns True when it was removed."""
    if is_blank(path):
        try:
            os.remove(path)
        except OSError:
            return False
        return True
    return False


def rotate_180(path):
    """Turn a picture upside down in place (north-up for Meteor passes heading north). True if done."""
    if Image is None:
        return False
    try:
        with Image.open(path) as im:
            out = im.rotate(180)
            out.save(path)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- how filled-in a satellite picture is
GOOD_FILL = 50.0          # a picture at least this filled in is worth keeping
_SKIP = ("map", "corrected", "project", "false_color", "composite_")


def scanline_fill(path):
    """How much of a satellite picture's scan lines hold data.

    A scan line the receiver missed comes out as a row of pure black. Counted between the first and the
    last line that has data (the start and end of a pass are low and weak, so they are not blamed on the
    antenna). Returns (fill_percent, lines_between_first_and_last) or None if the picture cannot be read.
    """
    if Image is None:
        return None
    try:
        with Image.open(path) as im:
            a = np.asarray(im.convert("RGB"))
    except Exception:
        return None
    if a.ndim != 3 or a.shape[0] < 20:
        return None
    black = (a.max(axis=2) == 0).mean(axis=1) >= 0.98          # rows that are (nearly) all pure black
    have = np.flatnonzero(~black)
    if have.size < 10:
        return 0.0, 0
    # Static is not a picture: a receiver that lost the signal still fills the lines with random speckle.
    # Real scan lines are smooth along the row (a pixel looks like its neighbour); noise is not.
    g = a.astype(np.float32).mean(axis=2)
    x0 = g[:, :-1] - g[:, :-1].mean(axis=1, keepdims=True)
    x1 = g[:, 1:] - g[:, 1:].mean(axis=1, keepdims=True)
    corr = (x0 * x1).sum(axis=1) / (np.sqrt((x0 ** 2).sum(axis=1) * (x1 ** 2).sum(axis=1)) + 1e-9)
    noisy = (g.std(axis=1) > 8) & (corr < 0.4)
    bad = black | noisy
    span = bad[have[0]:have[-1] + 1]
    return round(100.0 * (1.0 - float(span.mean())), 1), int(span.size)


def folder_fill(out_dir):
    """The fill figure for one pass's pictures: the median over the plain (uncorrected, no map) ones.

    Returns {"pct": ..., "lines": ...} or None when there is nothing to measure.
    """
    import glob
    files = []
    for f in glob.glob(os.path.join(out_dir, "**", "*.png"), recursive=True):
        name = os.path.basename(f).lower()
        if name.startswith("rgb") or "_rgb_" in name or "msu" in name:
            if not any(w in name for w in _SKIP):
                files.append(f)
    vals = [v for v in (scanline_fill(f) for f in sorted(files)[:24]) if v is not None and v[1] > 0]
    if not vals:
        return None
    vals.sort()
    pct, lines = vals[len(vals) // 2]
    # One median figure says how the pass went on average; what you want to know is how many pictures are worth
    # keeping.  A picture under GOOD_FILL is mostly black bands and static.
    good = sum(1 for p, _ln in vals if p >= GOOD_FILL)
    return {"pct": pct, "lines": lines, "good": good, "n": len(vals)}
