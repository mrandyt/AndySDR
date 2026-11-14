"""AndySDR self-update: looks for a newer GitHub Release, downloads it, installs it and restarts.

How it works
  * Checks  https://api.github.com/repos/<repo>/releases/latest  (public, no login) a little after
    start-up and then every 12 hours, plus whenever you press "Check for updates".
  * A pop-up in the GUI (on the radio PC and on any remote browser) tells you about the new version
    and asks what to do.  Nothing is downloaded or installed unless you say so - unless you tick
    "download automatically", in which case it only downloads and then asks before installing.
  * The download comes from this program's own GitHub releases only, is saved under
    %APPDATA%\\AndySDR\\updates, and is checked against its size and the SHA-256 that GitHub publishes.
  * Installed copy (made by AndySDR-Setup.exe): the installer is run silently and AndySDR restarts.
  * Portable copy (the unzipped folder): the new files are copied over the old ones and AndySDR restarts.
  * Running from source: you are shown the release page instead (git pull / re-download).
"""
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
import zipfile

from .config import APP_NAME, GITHUB_REPO, VERSION, app_dir, data_dir

log = logging.getLogger("andysdr.updater")

CHECK_EVERY = 12 * 3600           # seconds between automatic checks
FIRST_CHECK_DELAY = 25            # seconds after start-up (let the radio settle first)
INSTALLER_ASSET = "AndySDR-Setup.exe"
PORTABLE_ASSET = "AndySDR-windows.zip"
MAX_NOTES = 6000
UA = f"{APP_NAME}/{VERSION} (updater)"


def parse_version(text):
    """'v1.5.0' / '1.5' / '1.5.0-beta' -> (1, 5, 0).  Unparseable -> ()."""
    m = re.match(r"^\s*[vV]?(\d+(?:\.\d+){0,3})", str(text or ""))
    if not m:
        return ()
    parts = [int(p) for p in m.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer(latest, current):
    a, b = parse_version(latest), parse_version(current)
    return bool(a) and bool(b) and a > b


def install_mode():
    """installer = made by AndySDR-Setup.exe, portable = unzipped folder, source = python run_andysdr.py"""
    if not getattr(sys, "frozen", False):
        return "source"
    if os.path.exists(os.path.join(app_dir(), "unins000.exe")):
        return "installer"
    return "portable"


class Updater:
    def __init__(self, settings, notify, shutdown, api_url=None, allowed_prefix=None, mode=None):
        """notify(kind, payload) pushes to every client; shutdown() stops the radio before we exit."""
        self.s = settings
        self.notify = notify
        self.shutdown = shutdown
        self.api_url = api_url or f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        self.allowed_prefix = allowed_prefix or f"https://github.com/{GITHUB_REPO}/releases/download/"
        self.mode = mode or install_mode()
        self.dir = os.path.join(data_dir(), "updates")
        self.lock = threading.Lock()
        self.busy = False
        self.stop_flag = False
        self.state = "idle"        # idle | checking | uptodate | available | downloading | ready | installing | error
        self.error = ""
        self.release = None        # dict about the newest release (see _parse_release)
        self.progress = 0.0
        self.file = ""             # downloaded file (installer or zip)
        self.file_version = ""     # version that file belongs to
        self.manual = False        # the last check was asked for by the user
        self._last_prog = 0.0

    # ------------------------------------------------------------------ status
    def status(self):
        r = self.release or {}
        return {
            "state": self.state, "current": VERSION, "latest": r.get("version", ""),
            "notes": r.get("notes", ""), "name": r.get("name", ""), "url": r.get("page", ""),
            "size": r.get("size", 0), "mode": self.mode, "progress": round(self.progress, 3),
            "error": self.error, "manual": self.manual,
            "skipped": bool(r) and self.s.get("update_skip", "") == r.get("version"),
            "checked": self.s.get("update_last_check", 0),
            "can_install": self.mode in ("installer", "portable") and bool(r.get("asset_url")),
        }

    def _set(self, state, **kw):
        self.state = state
        for k, v in kw.items():
            setattr(self, k, v)
        self.notify("update", self.status())

    # ------------------------------------------------------------------ background loop
    def start(self):
        threading.Thread(target=self._loop, name="updater", daemon=True).start()

    def _loop(self):
        time.sleep(FIRST_CHECK_DELAY)
        while not self.stop_flag:
            try:
                last = float(self.s.get("update_last_check", 0) or 0)
                if self.s.get("update_check", True) and time.time() - last >= CHECK_EVERY - 60 \
                        and self.state in ("idle", "uptodate", "error", "available"):
                    self.check(manual=False, wait=True)
            except Exception:
                log.exception("update loop")
            for _ in range(600):           # wake up every 10 min to see whether a check is due
                if self.stop_flag:
                    return
                time.sleep(1)

    # ------------------------------------------------------------------ check
    def check(self, manual=True, wait=False):
        """Look for a newer release.  Runs in its own thread unless wait=True."""
        with self.lock:
            if self.busy or self.state in ("downloading", "installing"):
                return
            self.busy = True
        if not wait:
            threading.Thread(target=self._check, args=(manual,), name="update-check", daemon=True).start()
        else:
            self._check(manual)

    def _check(self, manual):
        auto_dl = False
        try:
            prev = self.state
            self.manual = manual
            if manual:
                self.s["update_skip"] = ""           # asking yourself overrides "skip this version"
                self._set("checking", error="")
            data = self._get_json(self.api_url)
            rel = self._parse_release(data)
            self.s["update_last_check"] = time.time()
            self.s.save()
            if rel and is_newer(rel["version"], VERSION):
                self.release = rel
                if prev == "ready" and self.file and os.path.exists(self.file) \
                        and self.file_version == rel["version"]:
                    self._set("ready")
                else:
                    self._set("available")
                    auto_dl = bool(self.s.get("update_auto_download", False) and rel["asset_url"]
                                   and self.mode != "source" and self.s.get("update_skip", "") != rel["version"])
                log.info("update available: %s (running %s)", rel["version"], VERSION)
            else:
                self.release = None
                self._set("uptodate")
        except Exception as e:
            log.warning("update check failed: %s", e)
            if manual:
                self._set("error", error=_friendly(e))
            else:
                self._set("idle")          # silent when offline - try again later
        finally:
            self.busy = False
        if auto_dl:
            self.download()

    def _get_json(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def _parse_release(self, d):
        if not isinstance(d, dict) or d.get("draft") or d.get("prerelease"):
            return None
        ver = str(d.get("tag_name", "")).lstrip("vV")
        if not parse_version(ver):
            return None
        want = INSTALLER_ASSET if self.mode == "installer" else PORTABLE_ASSET
        asset = None
        for a in d.get("assets", []):
            if a.get("name") == want:
                asset = a
        url = (asset or {}).get("browser_download_url", "")
        if url and not url.startswith(self.allowed_prefix):
            log.warning("ignoring release asset outside %s: %s", self.allowed_prefix, url)
            asset, url = None, ""
        digest = str((asset or {}).get("digest") or "")
        return {
            "version": ver, "name": d.get("name") or f"{APP_NAME} {ver}",
            "notes": str(d.get("body") or "")[:MAX_NOTES],
            "page": d.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases/latest",
            "asset_name": want if asset else "", "asset_url": url,
            "size": int((asset or {}).get("size") or 0),
            "sha256": digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else "",
        }

    # ------------------------------------------------------------------ download
    def download(self):
        with self.lock:
            if self.busy or self.state == "downloading" or not self.release or not self.release["asset_url"]:
                return
            self.busy = True
        threading.Thread(target=self._download, name="update-download", daemon=True).start()

    def _download(self):
        rel = self.release
        try:
            os.makedirs(self.dir, exist_ok=True)
            for f in os.listdir(self.dir):           # old downloads are no longer needed
                p = os.path.join(self.dir, f)
                if os.path.isfile(p):
                    os.remove(p)
            dest = os.path.join(self.dir, rel["asset_name"])
            part = dest + ".part"
            self._set("downloading", progress=0.0, error="")
            req = urllib.request.Request(rel["asset_url"], headers={"User-Agent": UA})
            h = hashlib.sha256()
            got = 0
            with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as out:
                total = int(r.headers.get("Content-Length") or rel["size"] or 0)
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    if total:
                        self._progress(got / total)
            if rel["size"] and got != rel["size"]:
                raise IOError(f"download incomplete ({got} of {rel['size']} bytes)")
            if rel["sha256"] and h.hexdigest() != rel["sha256"]:
                raise IOError("download failed its SHA-256 check - it was not installed")
            if dest.endswith(".zip") and not zipfile.is_zipfile(part):
                raise IOError("downloaded file is not a valid zip")
            os.replace(part, dest)
            self.file, self.file_version = dest, rel["version"]
            log.info("update %s downloaded to %s", rel["version"], dest)
            self._set("ready", progress=1.0)
        except Exception as e:
            log.exception("update download")
            self._set("error", error=_friendly(e))
            try:
                os.remove(os.path.join(self.dir, rel["asset_name"] + ".part"))
            except OSError:
                pass
        finally:
            self.busy = False

    def _progress(self, frac):
        self.progress = frac
        now = time.time()
        if now - self._last_prog > 0.25 or frac >= 1:
            self._last_prog = now
            self.notify("update_progress", {"progress": round(frac, 3)})

    # ------------------------------------------------------------------ skip / install
    def skip(self):
        if self.release:
            self.s["update_skip"] = self.release["version"]
            self.s.save()
            self.notify("update", self.status())

    def open_page(self):
        webbrowser.open(self.release["page"] if self.release else f"https://github.com/{GITHUB_REPO}/releases/latest")

    def install(self):
        """Run the downloaded update, then quit so the new version can replace this one."""
        if self.state != "ready" or not self.file or not os.path.exists(self.file):
            raise RuntimeError("No downloaded update is ready")
        if os.name != "nt":
            raise RuntimeError("Installing updates is only supported on Windows")
        relaunch = [a for a in sys.argv[1:] if a in ("--server", "--browser")]
        if self.mode == "installer":
            cmd = [self.file, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
                   "/RELAUNCH=1"]
            if relaunch:
                cmd.append("/LAUNCHARGS=" + relaunch[0])
            popen = dict(creationflags=0x00000008 | 0x00000200)           # DETACHED_PROCESS | NEW_PROCESS_GROUP
        elif self.mode == "portable":
            script = self._portable_script(relaunch)
            cmd = ["cmd", "/c", script]
            popen = dict(creationflags=0x08000000 | 0x00000008 | 0x00000200)   # NO_WINDOW | DETACHED | NEW_GROUP
        else:
            raise RuntimeError("This copy runs from source - update it with git pull or download the release")
        self._set("installing")
        log.info("installing update: %s", cmd)
        subprocess.Popen(cmd, close_fds=True, cwd=self.dir, **popen)
        threading.Thread(target=self._quit_soon, name="update-quit", daemon=True).start()

    def _quit_soon(self):
        time.sleep(1.5)                 # let the "installing" message reach the screen
        try:
            self.shutdown()
        except Exception:
            log.exception("shutdown before update")
        os._exit(0)

    def _portable_script(self, relaunch):
        """Unzip the new version and write a batch file that copies it over this folder after we exit."""
        target = os.path.abspath(app_dir())
        if not os.access(target, os.W_OK):
            raise RuntimeError(f"No permission to write to {target} - run AndySDR as administrator "
                               f"or unzip {self.file} yourself")
        stage = os.path.join(self.dir, "unpacked")
        shutil.rmtree(stage, ignore_errors=True)
        _safe_extract(self.file, stage)
        src = stage
        inner = os.path.join(stage, "AndySDR")
        if os.path.isfile(os.path.join(inner, "AndySDR.exe")):
            src = inner
        if not os.path.isfile(os.path.join(src, "AndySDR.exe")):
            raise RuntimeError("The update zip does not contain AndySDR.exe")
        exe = os.path.join(target, "AndySDR.exe")
        script = os.path.join(self.dir, "apply_update.cmd")
        lines = [
            "@echo off",
            "timeout /t 3 /nobreak >nul",
            # /IS /IT: replace files even if they look identical; retries wait for AndySDR to release them
            f'robocopy "{src}" "{target}" /E /IS /IT /R:30 /W:2 /NFL /NDL /NJH /NJS /NP >nul',
            "if errorlevel 8 goto failed",
            f'start "" "{exe}" {" ".join(relaunch)}',
            f'rmdir /s /q "{stage}"',
            "goto end",
            ":failed",
            f'echo Update failed - copy the files from "{src}" over "{target}" by hand > "{os.path.join(self.dir, "update_failed.txt")}"',
            f'start "" "{exe}" {" ".join(relaunch)}',
            ":end",
            "del \"%~f0\"",
        ]
        with open(script, "w", encoding="utf-8", newline="\r\n") as f:
            f.write("\n".join(lines) + "\n")
        return script


def _safe_extract(zpath, dest):
    os.makedirs(dest, exist_ok=True)
    root = os.path.realpath(dest)
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            out = os.path.realpath(os.path.join(dest, info.filename))
            if out != root and not out.startswith(root + os.sep):
                raise IOError("unsafe path in update zip")
        z.extractall(dest)


def _friendly(e):
    s = str(e)
    if "403" in s or "rate limit" in s.lower():
        return "GitHub is limiting requests right now - try again in a few minutes."
    if "404" in s:
        return "No published release was found on GitHub yet."
    if "urlopen error" in s or "timed out" in s.lower() or "getaddrinfo" in s:
        return "Could not reach GitHub - check the internet connection."
    return s[:300]
