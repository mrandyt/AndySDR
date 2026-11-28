"""Not calling something TETRA because it happens to be wide.

Taken from a real afternoon's scanning. Auto-identify announced TETRA on 165.1625, 168.0000, 454.0750, 463.6875
and 469.6375 MHz and started a decoder on each, which could never produce anything, because none of them is TETRA.
It also announced it on 380.7875 MHz, which is the UK emergency-services band and almost certainly real - and
which will never decode either, because public-safety TETRA in Europe encrypts the air interface, data included.

The measured numbers from that afternoon are the test cases. The false calls were 15.1 to 18.6 kHz wide; real
TETRA measured 20.7 kHz, because a 25 kHz channel is wider than a 12.5 kHz one however generously it is measured.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from andysdr import autodetect as A

fails = 0


def check(name, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name, "" if ok else extra)
    fails += 0 if ok else 1


def shape(bw, env, snr=22.0, duty=1.0, mode="NFM"):
    """Would a signal of this shape be called TETRA, at a frequency inside a TETRA band?"""
    return (A.TETRA_BW_MIN < bw < A.TETRA_BW_MAX and snr > 15 and A.TETRA_ENV_MIN < env < A.TETRA_ENV_MAX
            and duty > 0.75 and mode not in ("AM", "USB", "LSB", "CW"))


# --- the real readings from the log ---------------------------------------------------------------------------
real = [  # frequency Hz, bandwidth Hz, envelope variation, SNR dB, was it really TETRA
    (168_000_000, 15600, 0.22, 22.4, False),
    (454_075_000, 15400, 0.13, 22.2, False),
    (469_637_500, 15100, 0.38, 15.3, False),
    (469_637_500, 17500, 0.35, 15.0, False),
    (165_162_500, 18600, 4.40, 24.6, False),
    (463_687_500, 20600, 0.27, 24.5, None),    # in a TETRA band and the right shape: cannot be ruled out
    (380_787_500, 20700, 0.28, 37.1, True),    # the UK emergency-services band: real
]
for freq, bw, env, snr, truth in real:
    called = shape(bw, env, snr) and A._in_band(freq, A.TETRA_BANDS)
    if truth is False:
        check(f"{freq/1e6:10.4f} MHz, {bw/1000:.1f} kHz wide is no longer called TETRA", not called,
              f"bw={bw} env={env}")
    elif truth is True:
        check(f"{freq/1e6:10.4f} MHz, {bw/1000:.1f} kHz wide is still called TETRA", called, f"bw={bw} env={env}")

check("a 12.5 kHz channel measured generously cannot reach the bar", not shape(17500, 0.3))
check("a 25 kHz TETRA channel can", shape(20700, 0.28))
check("a bursty signal is not a carrier", not shape(18600, 4.4))
check("nor is a constant-envelope one", not shape(20700, 0.05))

# --- bands ------------------------------------------------------------------------------------------------------
check("the UK emergency-services band counts", A._in_band(380_787_500, A.TETRA_BANDS))
check("the commercial band counts", A._in_band(415_000_000, A.TETRA_BANDS))
check("VHF business radio does not", not A._in_band(168_000_000, A.TETRA_BANDS))
check("nor does 2 m", not A._in_band(145_500_000, A.TETRA_BANDS))
check("nor does the air band", not A._in_band(131_825_000, A.TETRA_BANDS))

# --- what it says about what will come out -----------------------------------------------------------------------
note = A._tetra_note(380_787_500)
check("it warns that the emergency-services band is encrypted", "encrypted" in note and "data" in note, note)
check("...and does not say that about a commercial frequency", "no voice or data will decode" not in
      A._tetra_note(415_000_000), A._tetra_note(415_000_000))

print("FAILED " + str(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
