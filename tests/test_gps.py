import time
from andysdr import gps


def cs(body):
    c = 0
    for ch in body:
        c ^= ord(ch)
    return f"${body}*{c:02X}"


GGA = cs("GPGGA,123519,5130.4446,N,00007.6680,W,1,08,0.9,34.5,M,46.9,M,,")
RMC = cs("GPRMC,123519,A,5130.4446,N,00007.6680,W,022.4,084.4,230394,003.1,W")
NOFIX = cs("GPGGA,123519,,,,,0,00,,,M,,M,,")


def test_checksum():
    assert gps.nmea_ok(GGA)
    assert not gps.nmea_ok(GGA[:-2] + "00")
    assert not gps.nmea_ok("garbage")


def test_parse_gga_and_rmc():
    d = gps.parse_nmea(GGA)
    assert d["fix"] and d["sats"] == 8 and d["alt"] == 34.5
    assert abs(d["lat"] - 51.50741) < 1e-4 and abs(d["lon"] - (-0.1278)) < 1e-4
    r = gps.parse_nmea(RMC)
    assert r["fix"] and abs(r["lat"] - 51.50741) < 1e-4 and r["speed_kn"] == 22.4


def test_no_fix():
    d = gps.parse_nmea(NOFIX)
    assert d["fix"] is False and "lat" not in d


class Settings(dict):
    def save(self):
        pass


class FakeSerial:
    def __init__(self, lines):
        self.lines = list(lines)
        self.closed = False

    def readline(self):
        if self.lines:
            return (self.lines.pop(0) + "\r\n").encode()
        time.sleep(0.05)
        return b""

    def close(self):
        self.closed = True


def test_link_applies_fix_once_then_on_move():
    got, events = [], []
    s = Settings(gps_enabled=True, gps_port="COM9", gps_baud=9600)
    link = gps.GpsLink(s, lambda k, p: events.append((k, p)), lambda la, lo, al: got.append((la, lo, al)),
                       opener=lambda port, baud: FakeSerial([NOFIX, GGA, RMC, GGA]))
    link.start()
    for _ in range(100):
        if got:
            break
        time.sleep(0.05)
    link.stop()
    assert len(got) == 1                      # same spot -> applied once
    assert abs(got[0][0] - 51.50741) < 1e-4 and got[0][2] == 34.5
    assert any(k == "gps" for k, _ in events)


def test_moved_far_reapplies():
    got = []
    link = gps.GpsLink(Settings(), lambda *a: None, lambda la, lo, al: got.append((la, lo)))
    link.handle(GGA)
    link.handle(cs("GPGGA,123520,5131.4446,N,00007.6680,W,1,08,0.9,34.5,M,46.9,M,,"))   # ~1.8 km north
    assert len(got) == 2


def test_no_ports_reports_status():
    s = Settings(gps_enabled=True, gps_port="auto", gps_baud=0)
    link = gps.GpsLink(s, lambda *a: None, lambda *a: None)
    link._candidates = lambda: []
    link.start()
    time.sleep(0.3)
    st = link.snapshot()["status"]
    link.stop()
    assert st == "no_port"


def test_daily_mode_releases_unit_after_fix():
    got, ser = [], []
    s = Settings(gps_enabled=True, gps_port="COM9", gps_baud=9600, gps_check_hours=24)

    def opener(port, baud):
        f = FakeSerial([GGA, GGA, GGA])
        ser.append(f)
        return f
    link = gps.GpsLink(s, lambda *a: None, lambda la, lo, al: got.append(1), opener=opener)
    link.start()
    for _ in range(100):
        if link.snapshot()["status"] == "waiting":
            break
        time.sleep(0.05)
    snap = link.snapshot()
    link.stop()
    assert snap["status"] == "waiting" and snap["next_check"] and got == [1] and ser[0].closed
