import numpy as np
from andysdr.dsp import Receiver, BLOCK_SECONDS


def _tone(fs):
    r = Receiver(fs); r.set_mode("USB"); r.squelch_db = -120
    n = int(fs * BLOCK_SECONDS); t = np.arange(n * 10) / fs
    x = np.exp(2j * np.pi * 31500 * t).astype(np.complex64)
    a = np.concatenate([np.asarray(r.process(x[i * n:(i + 1) * n], 30000)["audio"]) for i in range(10)][3:]).astype(float)
    return np.argmax(np.abs(np.fft.rfft(a * np.hanning(len(a))))) * 48000 / len(a)


def test_10msps_matches_2_4msps():
    assert abs(_tone(10_000_000) - 1500) < 20
    assert abs(_tone(2_400_000) - 1500) < 20


if __name__ == "__main__":
    test_10msps_matches_2_4msps(); print("ok")
