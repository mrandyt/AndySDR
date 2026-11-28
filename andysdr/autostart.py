"""Start AndySDR by itself when Windows is signed in to.

A scheduled satellite pass is only recorded if the program is running, so a Windows restart in the night (an update,
a power cut) can quietly cost a morning's pictures.  Turning this on puts AndySDR in the per-user "Run" list that
Windows reads at sign-in - the same list a program's own "start with Windows" tick uses.  Nothing is installed, no
administrator rights are needed, and turning it off removes the entry again.

It only works in the installed program (the .exe): run from source there is no single command for Windows to start.
Everything here is a no-op on anything that is not Windows.
"""
import logging
import os
import sys

log = logging.getLogger("andysdr.autostart")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "AndySDR"


def is_windows():
    return sys.platform.startswith("win")


def command():
    """The command Windows should run at sign-in, or None when there isn't one to offer."""
    if not is_windows() or not getattr(sys, "frozen", False):
        return None
    exe = os.path.abspath(sys.executable)
    if not exe.lower().endswith(".exe"):
        return None
    return '"%s"' % exe


def available():
    """Can this be offered at all?  (False when run from source, or not on Windows.)"""
    return command() is not None


def why_not():
    if not is_windows():
        return "This is a Windows setting."
    if not getattr(sys, "frozen", False):
        return "This only works in the installed AndySDR, not when it is run from the source code."
    return ""


def _winreg():
    import winreg                                   # noqa: PLC0415  (Windows only, and only when actually used)
    return winreg


def current():
    """What Windows has in its Run list for AndySDR right now: the command, or "" when there is none."""
    if not is_windows():
        return ""
    try:
        winreg = _winreg()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, VALUE_NAME)
            return str(val or "")
    except FileNotFoundError:
        return ""
    except OSError as e:                             # noqa: BLE001
        log.warning("start with Windows: could not read the Run list: %s", e)
        return ""


def enabled():
    """True when Windows is set to start this copy of AndySDR (an entry for some other copy does not count)."""
    cur = current()
    if not cur:
        return False
    want = command()
    if want is None:
        return bool(cur)
    return os.path.normcase(cur.strip().strip('"')) == os.path.normcase(want.strip().strip('"'))


def apply(on):
    """Add or remove the entry.  Returns (ok, message) - the message is shown to the person when something is wrong."""
    if not is_windows():
        return False, why_not()
    try:
        winreg = _winreg()
        if on:
            cmd = command()
            if cmd is None:
                return False, why_not()
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, cmd)
            log.info("start with Windows: on (%s)", cmd)
            return True, ""
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, VALUE_NAME)
        except FileNotFoundError:
            pass
        log.info("start with Windows: off")
        return True, ""
    except OSError as e:                             # noqa: BLE001
        log.warning("start with Windows: %s", e)
        return False, "Windows would not let that be changed: %s" % e


def sync(settings):
    """Called at start-up: make Windows agree with the saved setting.

    An update that reinstalls the program, or a copy moved to another folder, leaves the Run list pointing at a path
    that is no longer there; this puts the current path back without the person having to notice.
    """
    try:
        want = bool(settings.get("start_with_windows", False))
    except Exception:                                # noqa: BLE001
        return
    if not is_windows():
        return
    if want and not available():                     # run from source: leave whatever is there alone
        return
    try:
        if want != enabled():
            apply(want)
    except Exception:                                # noqa: BLE001
        log.exception("start with Windows")


def status():
    """What the screen shows: whether it is on, whether it can be offered, and why not."""
    return {"on": enabled() if is_windows() else False, "available": available(), "why": why_not(),
            "command": current()}
