"""Auto ID benchmark: confusion table of the classifier over synthetic signals at several signal strengths.
Run:  PYTHONPATH=. python3 tests/bench_autoid.py [--snr 8,12,16,22,30] [--trials 6]"""
import os
import sys
import argparse
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np  # noqa: E402
import autoid_signals as G  # noqa: E402
from andysdr.autodetect import SignalClassifier  # noqa: E402


def run(snrs, trials, only=None, verbose=True):
    total = good = 0
    rows = []
    for name, gen in G.CASES:
        if only and only not in name:
            continue
        for snr in snrs:
            seen = collections.Counter()
            for t in range(trials):
                rng = np.random.default_rng(1000 * t + int(snr) * 7 + len(name))
                iq, audio, mode, freq, truth = gen(rng, snr)
                r = SignalClassifier().classify(iq, audio, mode, freq)
                kind = r["kind"] if r else None
                if truth in ('AM voice', 'FM voice', 'SSB voice') and r and kind == 'FM voice' and r.get('mode') is None and truth != 'FM voice':
                    kind = 'undecided analogue'      # a weak or idle analogue signal: no mode named, nothing changed - not an error
                seen[kind] += 1
                total += 1
                good += (kind == truth)
            ok = seen.get(G_truth(name, gen), 0)
            rows.append((name, snr, seen))
            if verbose:
                truth = G_truth(name, gen)
                print(f"{name:26s} {snr:>3} dB  correct {seen.get(truth, 0)}/{trials}   " +
                      ", ".join(f"{k}:{v}" for k, v in seen.items() if k != truth))
    return good, total, rows


def G_truth(name, gen):
    return gen(np.random.default_rng(1), 20)[4]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--snr", default="10,15,22,30")
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--only", default=None)
    a = ap.parse_args()
    g, t, _ = run([float(x) for x in a.snr.split(",")], a.trials, a.only)
    print(f"\nOVERALL {g}/{t} = {100.0 * g / t:.1f}%")
