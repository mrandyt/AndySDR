"""ACARS and radiosonde decoders: round trips through a modulator built from the published formats."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from andysdr import acars, radiosonde  # noqa: E402


def run_acars(audio, chunk=2400):
    got = []
    dec = acars.AcarsDecoder(on_message=got.append)
    for i in range(0, len(audio), chunk):
        dec.process(audio[i:i + chunk])
    return got


def test_acars_crc_and_parse():
    chars, crc = acars.build_block(text="OUT 1234 OFF 1240", label="Q0", bid="3")
    assert acars.crc16(chars + crc) == 0
    m = acars.parse_block(chars)
    assert m["reg"] == "G-ABCD" and m["label"] == "Q0" and m["flight"] == "BA0123" and m["text"] == "OUT 1234 OFF 1240"


def test_acars_round_trip_clean():
    chars, crc = acars.build_block(text="POSITION N51234 W000123 FL350", label="H1")
    got = run_acars(acars.modulate(chars, crc))
    assert len(got) == 1 and got[0]["text"] == "POSITION N51234 W000123 FL350" and got[0]["reg"] == "G-ABCD"


def test_acars_noise_and_clock_error():
    chars, crc = acars.build_block(text="TEST MESSAGE WITH DRIFT", label="5Z", reg="G-XYZW")
    for drift in (-0.0004, 0.0004):
        got = run_acars(acars.modulate(chars, crc, noise=0.06, drift=drift))
        assert len(got) == 1 and got[0]["reg"] == "G-XYZW", drift


def test_acars_repairs_one_bad_character():
    chars, crc = acars.build_block(text="REPAIR ME PLEASE")
    bad = list(chars)
    bad[20] ^= 0x04                                        # one flipped bit: parity breaks, CRC breaks
    assert acars.AcarsDecoder._repair(bad, crc) == chars
    bad2 = list(chars)
    bad2[20] ^= 0x04
    bad2[25] ^= 0x10
    assert acars.AcarsDecoder._repair(bad2, crc) == chars
    bad3 = list(chars)
    for i in (5, 9, 14):
        bad3[i] ^= 0x01
    assert acars.AcarsDecoder._repair(bad3, crc) is None


def test_acars_silence_gives_nothing():
    assert run_acars(np.random.default_rng(3).normal(0, 0.01, 48000 * 3).astype(np.float32)) == []


def run_sonde(sig, chunk=2400):
    got = []
    dec = radiosonde.RadiosondeDecoder(on_sonde=got.append)
    for i in range(0, len(sig), chunk):
        dec.process(sig[i:i + chunk])
    return got


def test_rs41_frame_parse():
    fr = radiosonde.build_frame(sid="N1234567", frame_no=777, lat=51.5, lon=-0.12, alt=12345.0, vn=3.0, ve=-4.0, vu=5.5)
    r = radiosonde.parse_frame(fr)
    assert r["id"] == "N1234567" and r["frame"] == 777 and r["sats"] == 9
    assert abs(r["lat"] - 51.5) < 1e-4 and abs(r["lon"] + 0.12) < 1e-4 and abs(r["alt"] - 12345) < 0.1
    assert abs(r["vh"] - 5.0) < 0.05 and abs(r["vu"] - 5.5) < 0.05
    assert r["time"].startswith("2025-") or r["time"].startswith("2026-") or r["time"].startswith("2024-")


def test_rs41_bad_crc_rejected():
    fr = bytearray(radiosonde.build_frame())
    fr[0x120] ^= 0x40                                       # damage the position block
    assert radiosonde.parse_frame(bytes(fr)) is None


def test_rs41_round_trip_with_offset_noise_and_drift():
    fr = radiosonde.build_frame(sid="P2345678", lat=52.0, lon=-1.5, alt=8000.0)
    for drift, off in ((0.0, 0.0), (0.00015, 0.2), (-0.00015, -0.15)):
        got = run_sonde(radiosonde.modulate_frame(fr, noise=0.25, offset=off, drift=drift))
        assert len(got) == 1 and got[0]["id"] == "P2345678", (drift, off, len(got))
        assert abs(got[0]["lat"] - 52.0) < 1e-3


def test_rs41_inverted_signal():
    fr = radiosonde.build_frame(sid="Q3456789")
    got = run_sonde(-radiosonde.modulate_frame(fr, noise=0.1))
    assert len(got) == 1 and got[0]["id"] == "Q3456789"


def test_rs41_two_frames_in_a_row():
    a = radiosonde.modulate_frame(radiosonde.build_frame(frame_no=10, lead=0.0) if False else radiosonde.build_frame(frame_no=10), lead=0.1)
    b = radiosonde.modulate_frame(radiosonde.build_frame(frame_no=11), lead=0.35)
    got = run_sonde(np.concatenate((a, b)))
    assert [g["frame"] for g in got] == [10, 11]


if __name__ == "__main__":
    fails = 0
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            try:
                v()
                print("ok  ", k)
            except AssertionError as e:
                fails += 1
                print("FAIL", k, e)
    sys.exit(1 if fails else 0)
