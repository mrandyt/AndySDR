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
