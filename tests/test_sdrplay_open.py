import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from andysdr import sdrplay as sp

class FakeLib:
    def __init__(self, init_fail=0):
        self.calls = []; self.init_fail = init_fail
    def sdrplay_api_Open(self): self.calls.append("Open"); return 0
    def sdrplay_api_Close(self): self.calls.append("Close"); return 0
    def sdrplay_api_ApiVersion(self, v): return 0
    def sdrplay_api_LockDeviceApi(self): return 0
    def sdrplay_api_UnlockDeviceApi(self): return 0
    def sdrplay_api_GetDevices(self, devs, n, m):
        devs[0].SerNo = b"SER1"; n._obj.value = 1; return 0
    def sdrplay_api_SelectDevice(self, d): self.calls.append("Select"); return 0
    def sdrplay_api_ReleaseDevice(self, d): self.calls.append("Release"); return 0
    def sdrplay_api_Uninit(self, d): self.calls.append("Uninit"); return 0
    def sdrplay_api_Update(self, *a): return 0
    def sdrplay_api_Init(self, *a):
        self.calls.append("Init")
        if self.init_fail > 0:
            self.init_fail -= 1; return 1
        return 0

def fake_params(dev, p):
    return 0
def run(init_fail):
    lib = FakeLib(init_fail)
    sp._lib = lib; sp._open_count = 0
    sp._load = lambda: lib
    sp.time.sleep = lambda s: None
    d = sp.SdrplayDevice.__new__(sp.SdrplayDevice)
    d.serial = "SER1"; d.devT = None; d.params = None
    # stub the heavy middle: exercise open() wrapper with _open_inner replaced
    def inner(lib_):
        sp.DeviceT  # noqa
        d.devT = sp.DeviceT(); lib_.sdrplay_api_SelectDevice(None)
        for a in range(3):
            if lib_.sdrplay_api_Init() == 0: return
            lib_.sdrplay_api_Uninit(None)
        raise RuntimeError("fail")
    d._open_inner = inner
    try:
        d.open(); ok = True
    except RuntimeError:
        ok = False
    return ok, lib.calls, sp._open_count

ok, calls, cnt = run(2)
assert ok and cnt == 1, (ok, calls, cnt)
ok, calls, cnt = run(9)
assert not ok and cnt == 0 and "Release" in calls and calls[-1] == "Close", (ok, calls, cnt)
print("sdrplay open/abort ok")
