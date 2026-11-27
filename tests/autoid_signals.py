"""Synthetic test signals for the Auto ID classifier: what AndySDR hands it is 1 s of 48 kS/s complex baseband (iq48)
and the demodulated audio (audio_ungated).  Each generator returns (iq, audio, mode, freq_hz, truth) where truth is the
expected classifier 'kind' (None = nothing identifiable / noise)."""
import numpy as np
from scipy import signal

FS = 48000
N = 48000


def _noise(rng, n):
    return (rng.normal(size=n) + 1j * rng.normal(size=n)) / np.sqrt(2)


def _voice(rng, n=N):
    """Speech-like audio: band-limited noise (300-3000 Hz) with a slow syllable envelope, scaled to peak ~1."""
    x = signal.sosfilt(signal.butter(4, (300, 3000), "bandpass", fs=FS, output="sos"), rng.normal(size=n + 2000))[2000:]
    env = 0.55 + 0.45 * np.sin(2 * np.pi * (3 + rng.random() * 2) * np.arange(n) / FS + rng.random() * 6)
    x = x * env
    return (x / (np.max(np.abs(x)) + 1e-9)).astype(np.float64)


def _fm_demod(iq):
    d = np.angle(iq[1:] * np.conj(iq[:-1]), deg=False) * FS / (2 * np.pi)
    d = np.concatenate([[0], d])
    d = signal.sosfilt(signal.butter(4, 3400, "low", fs=FS, output="sos"), d)
    return (d / 3000.0).astype(np.float32)


def _with_noise(rng, sig, snr_db):
    """signal power -> noise so that (signal power)/(noise power in the 48 kHz channel) = snr_db"""
    p = np.mean(np.abs(sig) ** 2)
    nz = _noise(rng, len(sig)) * np.sqrt(p / 10 ** (snr_db / 10))
    return sig + nz


def _lp_channel(iq, bw=8500):
    return signal.lfilter(signal.firwin(129, bw, fs=FS), 1, iq)


def fm_voice(rng, snr, ctcss=None, dev=2500.0):
    a = _voice(rng)
    base = a * dev
    if ctcss:
        base = base + dev * 0.18 * np.sin(2 * np.pi * ctcss * np.arange(N) / FS)
    off = 150 + 250 * rng.random()
    iq = np.exp(1j * 2 * np.pi * (np.cumsum(base) + off * np.arange(N)) / FS)
    iq = _with_noise(rng, iq, snr)
    return iq.astype(np.complex64), _fm_demod(iq), "NFM", 155.5e6, "FM voice"


def am_voice(rng, snr, mode="AM"):
    a = _voice(rng)
    off = 150 + 250 * rng.random()
    iq = (1.0 + 0.7 * a) * np.exp(1j * (0.3 + 2 * np.pi * off * np.arange(N) / FS))
    iq = _with_noise(rng, iq, snr)
    audio = (np.abs(iq) - np.mean(np.abs(iq))).astype(np.float32)
    return iq.astype(np.complex64), audio, mode, 122.8e6, "AM voice"


def ssb_voice(rng, snr, sideband="USB"):
    a = _voice(rng)
    z = signal.hilbert(a)
    iq = (z if sideband == "USB" else np.conj(z)) * 0.7
    iq = _with_noise(rng, iq, snr)
    audio = np.real(iq).astype(np.float32)
    return iq.astype(np.complex64), audio, sideband, 14.2e6 if sideband == "USB" else 7.1e6, "SSB voice"


def _fsk_iq(rng, rate, levels, dev_outer, n=N, rolloff=True):
    """Continuous M-FSK at `rate` baud; symbols uniformly random over `levels` equally spaced deviations spanning
    +-dev_outer.  Gaussian-ish pulse shaping via the channel filter."""
    nsym = int(np.ceil(n * rate / FS)) + 4
    k = int(rng.integers(0, 1 << 30))
    r = np.random.default_rng(k)
    s = r.integers(0, levels, nsym)
    amp = (s - (levels - 1) / 2) / ((levels - 1) / 2) * dev_outer
    f = np.repeat(amp, int(np.ceil(FS / rate)) + 1)  # coarse; resample below to exact rate
    t = np.arange(n) * rate / FS
    f = amp[np.minimum(t.astype(int), nsym - 1)]
    if rolloff:
        f = signal.sosfilt(signal.butter(2, rate * 0.55, "low", fs=FS, output="sos"), f)
    return np.exp(1j * 2 * np.pi * np.cumsum(f) / FS)


def dmr_burst(rng, snr):
    """DMR handset: 4FSK 4800 Bd, +-1944 Hz outer, bursts of 27.5 ms every 60 ms."""
    iq = _fsk_iq(rng, 4800, 4, 1944.0)
    t = np.arange(N) / FS
    gate = ((t % 0.060) < 0.0275).astype(float)
    gate = signal.sosfilt(signal.butter(2, 800, "low", fs=FS, output="sos"), gate)
    iq = iq * np.clip(gate, 0, 1)
    iq = _with_noise(rng, iq, snr)
    return iq.astype(np.complex64), _fm_demod(iq), "DIG", 438.5e6, "DMR"


def p25_cont(rng, snr, freq=165.05e6):
    iq = _fsk_iq(rng, 4800, 4, 1800.0)
    iq = _with_noise(rng, iq, snr)
    return iq.astype(np.complex64), _fm_demod(iq), "DIG", freq, "P25 / DMR"


def ysf_cont(rng, snr):
    iq = _fsk_iq(rng, 4800, 4, 1800.0)
    iq = _with_noise(rng, iq, snr)
    return iq.astype(np.complex64), _fm_demod(iq), "DIG", 438.6e6, "Digital 4FSK"


def nxdn48(rng, snr, freq=460.2e6):
    iq = _fsk_iq(rng, 2400, 4, 1050.0)
    iq = _with_noise(rng, iq, snr)
    return iq.astype(np.complex64), _fm_demod(iq), "DIG", freq, "NXDN / dPMR"


def dpmr446(rng, snr):
    iq = _fsk_iq(rng, 2400, 4, 1050.0)
    iq = _with_noise(rng, iq, snr)
    return iq.astype(np.complex64), _fm_demod(iq), "DIG", 446.1e6, "dPMR"


def pocsag(rng, snr, rate=1200, dev=4500.0):
    iq = _fsk_iq(rng, rate, 2, dev)
    iq = _with_noise(rng, iq, snr)
    return iq.astype(np.complex64), _fm_demod(iq), "NFM", 153.35e6, "Pager"


def flex(rng, snr):
    iq = _fsk_iq(rng, 1600, 2, 4800.0)
    iq = _with_noise(rng, iq, snr)
    return iq.astype(np.complex64), _fm_demod(iq), "NFM", 153.1e6, "Pager"


def _afsk(rng, bits, n=N):
    sym = np.repeat(bits, FS // 1200)[:n]
    f = np.where(sym > 0, 1200.0, 2200.0)
    return np.sin(2 * np.pi * np.cumsum(f) / FS)


def aprs(rng, snr):
    a = _afsk(rng, rng.integers(0, 2, N * 1200 // FS + 2))      # a full second of packet data
    iq = np.exp(1j * 2 * np.pi * np.cumsum(a * 2500.0) / FS)
    iq = _with_noise(rng, iq, snr)
    return iq.astype(np.complex64), _fm_demod(iq), "NFM", 144.8e6, "APRS"


def dtmf(rng, snr):
    seq = np.zeros(N)
    pairs = [(697, 1209), (770, 1336), (852, 1477), (941, 1209)]
    seg = N // 4
    for i, (lo, hi) in enumerate(pairs):
        tt = np.arange(seg) / FS
        seq[i * seg:(i + 1) * seg] = 0.5 * (np.sin(2 * np.pi * lo * tt) + np.sin(2 * np.pi * hi * tt))
    iq = np.exp(1j * 2 * np.pi * np.cumsum(seq * 2500.0) / FS)
    iq = _with_noise(rng, iq, snr)
    return iq.astype(np.complex64), _fm_demod(iq), "NFM", 446.0e6, "DTMF"


def tetra(rng, snr):
    """pi/4-DQPSK 18 kBd, root-raised-cosine-ish, wide in the 48 kHz channel."""
    sps = 8
    nsym = int(N / FS * 18000) + 8
    ph = np.cumsum(rng.integers(0, 4, nsym) * (np.pi / 2) + np.pi / 4)
    sym = np.exp(1j * ph)
    t = np.arange(N) * 18000 / FS
    x = sym[np.minimum(t.astype(int), nsym - 1)]
    x = signal.lfilter(signal.firwin(65, 11000, fs=FS), 1, x)
    iq = _with_noise(rng, x, snr)
    return iq.astype(np.complex64), _fm_demod(iq), "DIG25", 392.0e6, "TETRA"


def noise_only(rng, snr=0):
    iq = _noise(rng, N) * 0.01
    return iq.astype(np.complex64), _fm_demod(iq), "NFM", 150.0e6, None


CASES = [
    ("FM voice", lambda r, s: fm_voice(r, s)),
    ("FM voice + CTCSS 88.5", lambda r, s: fm_voice(r, s, ctcss=88.5)),
    ("FM voice + CTCSS 233.6", lambda r, s: fm_voice(r, s, ctcss=233.6)),
    ("FM voice wide dev 4k", lambda r, s: fm_voice(r, s, dev=4000.0)),
    ("AM voice (AM mode)", lambda r, s: am_voice(r, s, "AM")),
    ("USB voice", lambda r, s: ssb_voice(r, s, "USB")),
    ("LSB voice", lambda r, s: ssb_voice(r, s, "LSB")),
    ("DMR handset", dmr_burst),
    ("P25/DMR continuous", p25_cont),
    ("4FSK 70cm amateur", ysf_cont),
    ("NXDN48", nxdn48),
    ("dPMR446", dpmr446),
    ("POCSAG 512", lambda r, s: pocsag(r, s, 512)),
    ("POCSAG 1200", lambda r, s: pocsag(r, s, 1200)),
    ("POCSAG 2400", lambda r, s: pocsag(r, s, 2400)),
    ("FLEX 1600", flex),
    ("APRS 1200", aprs),
    ("DTMF", dtmf),
    ("TETRA", tetra),
    ("noise only", lambda r, s: noise_only(r, s)),
]
