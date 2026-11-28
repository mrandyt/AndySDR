"""Start with Windows: the entry Windows reads at sign-in is added, removed and repaired, and never touched elsewhere."""
import os, sys, types
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from andysdr import autostart

fails = 0
def check(n, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + n + (" " + str(extra) if extra and not ok else ""))
    fails += 0 if ok else 1


class FakeKey:
    def __init__(self, store, exists=True):
        self.store, self.exists = store, exists
    def __enter__(self):
        if not self.exists:
            raise FileNotFoundError("no such key")
        return self
    def __exit__(self, *a):
        return False


def fake_winreg(store, key_exists=True):
    """Just enough of the winreg module: one key holding one dict of values."""
    m = types.SimpleNamespace(HKEY_CURRENT_USER=1, REG_SZ=1, KEY_SET_VALUE=2)
    m.OpenKey = lambda root, path, res=0, access=0: FakeKey(store, key_exists)
    m.CreateKey = lambda root, path: FakeKey(store)
    def query(k, name):
        if name not in k.store:
            raise FileNotFoundError(name)
        return k.store[name], 1
    def setv(k, name, res, typ, val):
        k.store[name] = val
    def delv(k, name):
        if name not in k.store:
            raise FileNotFoundError(name)
        del k.store[name]
    m.QueryValueEx, m.SetValueEx, m.DeleteValue = query, setv, delv
    return m


def use(store, exe=r"C:\Program Files\AndySDR\AndySDR.exe", windows=True, frozen=True, key_exists=True):
    autostart.is_windows = lambda: windows
    autostart.command = lambda: ('"%s"' % exe) if (windows and frozen) else None
    autostart.available = lambda: autostart.command() is not None
    autostart._winreg = lambda: fake_winreg(store, key_exists)


EXE = r"C:\Program Files\AndySDR\AndySDR.exe"

store = {}
use(store)
check("it starts off", autostart.enabled() is False)
ok, why = autostart.apply(True)
check("turning it on writes the entry", ok and store.get("AndySDR") == '"%s"' % EXE, (ok, why, store))
check("and it then reads as on", autostart.enabled() is True)
ok, _ = autostart.apply(False)
check("turning it off removes it", ok and "AndySDR" not in store, store)
check("turning it off twice is not an error", autostart.apply(False)[0] is True)

store = {"AndySDR": r'"D:\Old\AndySDR.exe"', "OtherApp": "something else"}
use(store)
check("an entry for another copy does not count as on", autostart.enabled() is False)
autostart.sync({"start_with_windows": True})
check("start-up repairs a stale path", store["AndySDR"] == '"%s"' % EXE, store)
check("and leaves other programs alone", store["OtherApp"] == "something else")

store = {"AndySDR": '"%s"' % EXE}
use(store)
autostart.sync({"start_with_windows": False})
check("start-up removes it when the setting is off", "AndySDR" not in store, store)

store = {"AndySDR": '"%s"' % EXE}
use(store, frozen=False)
autostart.sync({"start_with_windows": True})
check("run from source it leaves the list alone", store.get("AndySDR") == '"%s"' % EXE, store)

store = {}
use(store, windows=False)
check("on anything but Windows it is simply off", autostart.enabled() is False and autostart.apply(True)[0] is False)
autostart.sync({"start_with_windows": True})
check("and start-up writes nothing", store == {}, store)

store = {}
use(store, key_exists=False)
check("a missing Run key reads as off, not an error", autostart.current() == "")

print("FAILED " + str(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
