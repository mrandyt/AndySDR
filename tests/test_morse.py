import numpy as np
from andysdr.morse import MorseDecoder, MORSE

REV = {v: k for k, v in MORSE.items()}


def synth(text, wpm=18, tone=650, snr_db=20, fs=48000, lead=0.8):
    u = 1.2 / wpm
    on = []
    for word in text.split(" "):
        for ch in word:
            for s in REV[ch]:
                on.append((1, u if s == "." else 3 * u)); on.append((0, u))
            on.append((0, 2 * u))
        on.append((0, 4 * u))
    t_total = lead + sum(d for _, d in on) + 1.5
    x = np.zeros(int(t_total * fs), np.float32)
    t = lead
    for st, d in on:
        if st:
            a, b = int(t * fs), int((t + d) * fs)
            n = np.arange(b - a)
            env = np.minimum(1, np.minimum(n, b - a - n) / (0.004 * fs))
            x[a:b] = env * np.sin(2 * np.pi * tone * n / fs)
        t += d
    rng = np.random.default_rng(1)
    return x * 0.5 + rng.normal(0, 0.5 * 10 ** (-snr_db / 20), len(x)).astype(np.float32)


def run(text, **kw):
    out = []
    d = MorseDecoder(on_text=out.append)
    x = synth(text, **kw)
    for i in range(0, len(x), 2400):
        d.process(x[i:i + 2400])
    return "".join(out).strip(), d


def test_clean():
    got, d = run("CQ CQ DE G4ABC K")
    assert got == "CQ CQ DE G4ABC K", got


def test_speeds_and_noise():
    for wpm in (10, 25, 35):
        got, d = run("CQ TEST 73 DE M0XYZ", wpm=wpm, snr_db=14)
        assert got.replace(" ", "") == "CQTEST73DEM0XYZ", (wpm, got)


def test_pitch():
    got, d = run("PARIS", tone=900)
    assert got == "PARIS" and abs(d.tone - 900) < 25


if __name__ == "__main__":
    for f in (test_clean, test_speeds_and_noise, test_pitch):
        f(); print("PASS", f.__name__)
