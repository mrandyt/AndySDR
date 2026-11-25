"""RTL-SDR continuous reading: no gaps however slowly the samples are processed, and it falls back cleanly."""
import ctypes, os, sys, tempfile, threading, time
tmp = tempfile.mkdtemp()
os.environ["HOME"] = tmp
os.environ["APPDATA"] = tmp
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from andysdr import devices

fails = 0
def check(n, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + n + (" " + str(extra) if extra and not ok else ""))
    fails += 0 if ok else 1


class FakeLib:
    """Stands in for librtlsdr: read_async calls the callback with consecutive numbered buffers until cancelled."""
    def __init__(self, chunk=4096, interval=0.002, fail_async=False):
        self.chunk, self.interval, self.fail_async = chunk, interval, fail_async
        self.cancel = threading.Event()
        self.cancelled = 0
        self.sent = 0                      # bytes handed to the callback so far
        self.closed = 0
        # real ctypes functions accept .argtypes, so these are plain functions rather than bound methods
        self.rtlsdr_read_async = lambda *a: self._read_async(*a)
        self.rtlsdr_cancel_async = lambda *a: self._cancel(*a)

    def _read_async(self, handle, cb, ctx, buf_num, buf_len):
        if self.fail_async:
            return -1
        self.cancel.clear()
        while not self.cancel.is_set():
            buf = (ctypes.c_ubyte * self.chunk)()
            for i in range(0, self.chunk, 2):          # I = low byte of the running sample number, Q = high byte
                n = (self.sent + i) // 2
                buf[i], buf[i + 1] = n & 0xFF, (n >> 8) & 0xFF
            cb(ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)), self.chunk, None)
            self.sent += self.chunk
            time.sleep(self.interval)
        return 0

    def _cancel(self, handle):
        self.cancelled += 1
        self.cancel.set()
        return 0

    def rtlsdr_close(self, handle):
        self.closed += 1


def make(lib):
    devices.rtl_lib = lambda: lib
    d = devices.RtlNativeDevice(0, "TEST", "Fake")
    d.dev = ctypes.c_void_p(1)
    d.sample_rate = 1200000
    d._buf = (ctypes.c_uint8 * (1 << 18))()
    return d


def unbroken(x):
    """True if the samples count up one by one (the fake's pattern), i.e. nothing was lost between reads."""
    raw = np.round(x.real * 128.0 + 127.4).astype(int) & 0xFF, np.round(x.imag * 128.0 + 127.4).astype(int) & 0xFF
    n = raw[0] + (raw[1] << 8)
    return bool(np.all(np.diff(n) % 65536 == 1))


# 1. a slow consumer still gets every sample, in order
lib = FakeLib()
d = make(lib)
d.async_read = True
got = []
t0 = time.time()
while time.time() - t0 < 1.0:
    got.append(d.read(3000))
    time.sleep(0.02)                       # "processing" takes far longer than the dongle needs to fill a block
x = np.concatenate([g for g in got if len(g)])
check("reads return samples", len(x) > 3000, len(x))
check("a slow consumer loses nothing (samples stay consecutive)", unbroken(x))
info = d.stream_info()
check("stream_info reports what arrived", info is not None and info["got_s"] > 0 and info["dropped_s"] == 0, info)
d.close()
check("close cancels the stream and joins the thread", lib.cancelled >= 1 and d._thr is None)
check("close closes the dongle", lib.closed == 1)

# 2. when the processing falls hopelessly behind the oldest data is dropped and counted, not grown without limit
lib = FakeLib(chunk=65536, interval=0.0005)
d = make(lib)
d.sample_rate = 250000
d._STREAM_SECONDS = 1                      # cap = 500 kB
d.async_read = True
d.read(10)                                 # start the stream
time.sleep(0.6)
check("the backlog stays under its cap", d._qbytes <= 500000 + 65536, d._qbytes)
check("dropped data is counted", d._dropped > 0, d._dropped)
d.close()

# 3. sample-rate change restarts the stream by itself
lib = FakeLib()
d = make(lib)
d.async_read = True
d.read(100)
first = d._thr
lib.rtlsdr_set_sample_rate = lambda h, r: 0
lib.rtlsdr_reset_buffer = lambda h: 0
d.set_sample_rate(1200000)
check("set_sample_rate stops the old stream", first is not None and not first.is_alive() and d._thr is None)
y = d.read(100)
check("the next read starts it again", len(y) > 0 and d._thr is not None)
d.close()

# 4. the plain reader is still used when asked, and when the library cannot stream
lib = FakeLib(fail_async=True)
lib.rtlsdr_read_sync = lambda h, buf, n, got: -1
d = make(lib)
d.async_read = True
r = d.read(100)
check("a failing async start falls back to the plain reader (no crash)", len(r) == 0 and d.async_read is False)
d.close()
d = make(FakeLib())
check("async_read is off by default", devices.RtlNativeDevice.async_read is False and d.async_read is False)

print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
