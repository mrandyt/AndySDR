"""Who is that?  DMR radio ID -> callsign / name / place, and talkgroup names.

The free RadioID.net list of registered DMR users (about 250,000 people) is downloaded by the PC by itself:

  * the first time it is needed (shortly after AndySDR starts),
  * then again whenever the copy on disk is more than 28 days old (checked every few hours while the program runs),
  * or straight away when you press "Update callsign list now" on the DECODERS page.

It is kept in a small SQLite file in %APPDATA%\\AndySDR\\dmr\\.  If the download fails (no internet, site down) the old
copy is kept and another try is made a few hours later, so a failed download never loses what you already have.

Talkgroup names come from, in this order: talkgroups you imported from RadioReference, the BrandMeister list
(downloaded together with the callsigns) and a few built-in ones.

The phone / tablet app needs nothing of its own: it shows the PC's web page, so the names reach it through the PC.
"""
import csv
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.request

from .config import VERSION, data_dir

log = logging.getLogger("andysdr.dmrdb")

USER_URLS = ("https://database.radioid.net/static/user.csv",
             "https://www.radioid.net/static/user.csv")
JSON_URL = "https://www.radioid.net/static/users.json"          # older, bigger, used only if the CSV cannot be fetched
TG_URL = "https://api.brandmeister.network/v2/talkgroup"
STALE_DAYS = 28
RETRY_HOURS = 6
CHECK_EVERY_S = 3 * 3600
FIRST_DELAY_S = 20
MIN_ROWS = 1000          # a "database" smaller than this is a broken download and is thrown away

# A few that are the same everywhere.  (The BrandMeister list overrides these once it has been downloaded.)
BUILTIN_TG = {9: "Local", 91: "World-wide", 92: "Europe", 93: "North America", 4000: "Disconnect", 9990: "Parrot (echo test)"}

_ID = ("radio_id", "id", "dmr_id", "dmrid", "radioid", "user_id")
_CALL = ("callsign", "call", "call_sign")
_FIRST = ("first_name", "fname", "firstname", "first", "name")
_LAST = ("last_name", "surname", "lname", "lastname", "last")
_CITY = ("city", "town")
_STATE = ("state", "region", "province")
_COUNTRY = ("country",)


def _pick(row, names):
    for n in names:
        if n in row and row[n] is not None and str(row[n]).strip():
            return str(row[n]).strip()
    return ""


def _record(row):
    """row: dict with lower-case keys -> (id, callsign, name, place) or None."""
    try:
        rid = int(_pick(row, _ID))
    except ValueError:
        return None
    call = _pick(row, _CALL).upper()
    if rid <= 0 or not call:
        return None
    name = " ".join(x for x in (_pick(row, _FIRST), _pick(row, _LAST)) if x)
    place = ", ".join(x for x in (_pick(row, _CITY), _pick(row, _COUNTRY)) if x)
    return rid, call, name, place


def parse_csv(lines):
    """Yields (id, callsign, name, place).  Reads the column names from the first line when it has some; a file
    without a header is taken as the classic order  id, callsign, first name, last name, city, state, country."""
    rdr = csv.reader(lines)
    head = None
    for cells in rdr:
        if not cells:
            continue
        if head is None:
            low = [c.strip().lower().lstrip("﻿") for c in cells]
            if any(n in low for n in _ID):
                head = low
                continue
            head = ["radio_id", "callsign", "first_name", "last_name", "city", "state", "country"]
        row = {head[i]: cells[i] for i in range(min(len(head), len(cells)))}
        rec = _record(row)
        if rec:
            yield rec


def parse_json(obj):
    items = obj.get("users", obj.get("results", [])) if isinstance(obj, dict) else obj
    for it in items or []:
        if isinstance(it, dict):
            rec = _record({str(k).lower(): v for k, v in it.items()})
            if rec:
                yield rec


def parse_talkgroups(obj):
    """BrandMeister: {"91": "World-wide", ...}  (a list of {id,name} objects is accepted too)."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict):
                v = v.get("Name") or v.get("name")
            try:
                tg = int(k)
            except (TypeError, ValueError):
                continue
            if isinstance(v, str) and v.strip():
                out[tg] = v.strip()
    elif isinstance(obj, list):
        for it in obj:
            if isinstance(it, dict):
                try:
                    tg = int(it.get("id", it.get("TG", it.get("tg"))))
                except (TypeError, ValueError):
                    continue
                v = it.get("name") or it.get("Name")
                if isinstance(v, str) and v.strip():
                    out[tg] = v.strip()
    return out


class DmrDb:
    def __init__(self, settings, emit=None, folder=None, memory_tg=None, clock=time.time, opener=None):
        self.s = settings
        self.emit = emit or (lambda *a: None)
        self.dir = folder or os.path.join(data_dir(), "dmr")
        os.makedirs(self.dir, exist_ok=True)
        self.db_path = os.path.join(self.dir, "radioid_users.sqlite")
        self.tg_path = os.path.join(self.dir, "talkgroups.json")
        self.state_path = os.path.join(self.dir, "state.json")
        self.memory_tg = memory_tg or (lambda: {})        # () -> {tgid: name} imported from RadioReference
        self.clock = clock
        self._open = opener or self._default_open        # (url) -> binary file-like object
        self._lock = threading.RLock()
        self._con = None
        self._cache = {}
        self._busy = False
        self._msg = ""
        self._stop = threading.Event()
        self.tg = {}
        self._state = {}
        self._load_state()
        self._load_tg()

    # ------------------------------------------------------------------ small files
    def _load_state(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                self._state = json.load(f)
        except (OSError, ValueError):
            self._state = {}

    def _save_state(self):
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self._state, f)
        except OSError as e:
            log.debug("state not saved: %s", e)

    def _load_tg(self):
        try:
            with open(self.tg_path, "r", encoding="utf-8") as f:
                self.tg = {int(k): v for k, v in json.load(f).items()}
        except (OSError, ValueError):
            self.tg = {}

    # ------------------------------------------------------------------ lookups
    def _connection(self):
        if self._con is None and os.path.exists(self.db_path):
            self._con = sqlite3.connect(self.db_path, check_same_thread=False)
        return self._con

    def lookup(self, rid):
        """-> {"call","name","place"} for a DMR ID, or None when it is unknown / the list is not downloaded yet."""
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            return None
        with self._lock:
            if rid in self._cache:
                return self._cache[rid]
            try:
                con = self._connection()
                row = con.execute("SELECT call, name, place FROM users WHERE id=?", (rid,)).fetchone() if con else None
            except sqlite3.Error as e:
                log.warning("callsign list unreadable: %s", e)
                self._close()
                row = None
            res = {"call": row[0], "name": row[1], "place": row[2]} if row else None
            if len(self._cache) > 5000:
                self._cache.clear()
            self._cache[rid] = res
            return res

    def tg_name(self, tg):
        try:
            tg = int(tg)
        except (TypeError, ValueError):
            return ""
        try:
            mine = self.memory_tg() or {}
        except Exception:      # noqa: BLE001
            mine = {}
        return mine.get(tg) or self.tg.get(tg) or BUILTIN_TG.get(tg, "")

    def _close(self):
        with self._lock:
            if self._con is not None:
                try:
                    self._con.close()
                except sqlite3.Error:
                    pass
                self._con = None

    # ------------------------------------------------------------------ status
    def status(self):
        count = updated = 0
        with self._lock:
            try:
                con = self._connection()
                if con:
                    count = con.execute("SELECT value FROM meta WHERE key='rows'").fetchone()
                    updated = con.execute("SELECT value FROM meta WHERE key='updated'").fetchone()
                    count = int(count[0]) if count else 0
                    updated = float(updated[0]) if updated else 0.0
            except (sqlite3.Error, ValueError):
                count = updated = 0
        if self._busy:
            state = "downloading"
        elif count:
            state = "ready"
        elif self._state.get("error"):
            state = "error"
        else:
            state = "missing"
        age = (self.clock() - updated) / 86400.0 if updated else None
        return {"state": state, "count": count, "updated": updated, "age_days": None if age is None else round(age, 1),
                "tg_count": len(self.tg), "msg": self._msg or self._state.get("error", ""),
                "auto": self._auto()}

    def _push(self):
        try:
            self.emit("dmrdb", self.status())
        except Exception:      # noqa: BLE001
            pass

    def _auto(self):
        v = self.s.get("dmr_db_auto", True)
        return True if v is None else bool(v)

    # ------------------------------------------------------------------ downloading
    def _default_open(self, url):
        from .updater import _urlopen
        req = urllib.request.Request(url, headers={"User-Agent": f"AndySDR/{VERSION}"})
        return _urlopen(req, 60)

    def _urls(self):
        custom = str(self.s.get("dmr_db_url", "") or "").strip()
        return [custom] if custom else list(USER_URLS)

    def due(self):
        """Is a download worth trying right now?  (Only asks about time - the auto setting is checked by the caller.)"""
        now = self.clock()
        if now - float(self._state.get("last_try", 0)) < RETRY_HOURS * 3600:
            return False
        st = self.status()
        return st["state"] in ("missing", "error") or (st["age_days"] is not None and st["age_days"] >= STALE_DAYS)

    def refresh(self, force=False, wait=False):
        """Starts a download in the background (or runs it here with wait=True).  Returns False if one is already running."""
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self._msg = "Downloading the callsign list ..."
        self._push()
        if wait:
            self._run()
        else:
            threading.Thread(target=self._run, daemon=True, name="dmrdb").start()
        return True

    def _run(self):
        ok, err = False, ""
        try:
            ok, err = self._download_users()
            self._download_tg()
        except Exception as e:      # noqa: BLE001
            err = str(e)
            log.warning("callsign list update failed: %s", e)
        finally:
            with self._lock:
                self._busy = False
                self._state["last_try"] = self.clock()
                if ok:
                    self._state.pop("error", None)
                    self._msg = ""
                else:
                    self._state["error"] = err or "download failed"
                    self._msg = ""
                self._save_state()
        self._push()
        if ok:
            self.emit("event", {"level": "info", "text": f"DMR callsign list updated ({self.status()['count']:,} people)"})
        elif self.status()["state"] != "ready":
            self.emit("event", {"level": "warn", "text": "Could not download the DMR callsign list: " + (err or "unknown error")})

    def _download_users(self):
        last = "no address to try"
        for url in self._urls():
            try:
                with self._open(url) as resp:
                    n = self._ingest(resp, url)
                log.info("callsign list: %d people from %s", n, url)
                return True, ""
            except Exception as e:      # noqa: BLE001
                try:
                    from .updater import _friendly
                    last = _friendly(e)
                except Exception:       # noqa: BLE001
                    last = str(e)
                log.info("callsign list from %s failed: %s", url, e)
        try:                                                   # last resort: the big JSON file
            with self._open(JSON_URL) as resp:
                n = self._ingest(resp, JSON_URL, as_json=True)
            log.info("callsign list: %d people from %s", n, JSON_URL)
            return True, ""
        except Exception as e:          # noqa: BLE001
            log.info("callsign list JSON failed: %s", e)
        return False, last

    def _ingest(self, resp, url, as_json=False):
        tmp = self.db_path + ".new"
        if os.path.exists(tmp):
            os.remove(tmp)
        con = sqlite3.connect(tmp)
        try:
            con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, call TEXT, name TEXT, place TEXT)")
            con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            if as_json:
                recs = parse_json(json.loads(resp.read().decode("utf-8", "replace")))
            else:
                recs = parse_csv(line.decode("utf-8", "replace") for line in resp)
            n, batch = 0, []
            for rec in recs:
                batch.append(rec)
                if len(batch) >= 5000:
                    con.executemany("INSERT OR REPLACE INTO users VALUES (?,?,?,?)", batch)
                    n += len(batch)
                    batch = []
            if batch:
                con.executemany("INSERT OR REPLACE INTO users VALUES (?,?,?,?)", batch)
                n += len(batch)
            if n < MIN_ROWS:
                raise ValueError(f"the file had only {n} people - not the real list")
            con.executemany("INSERT INTO meta VALUES (?,?)", [("rows", str(n)), ("updated", str(self.clock())), ("source", url)])
            con.commit()
        except Exception:
            con.close()
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        con.close()
        with self._lock:                                      # swap the new file in (Windows will not replace an open file)
            self._close()
            self._cache.clear()
            os.replace(tmp, self.db_path)
        return n

    def _download_tg(self):
        try:
            with self._open(TG_URL) as resp:
                tg = parse_talkgroups(json.loads(resp.read().decode("utf-8", "replace")))
        except Exception as e:      # noqa: BLE001
            log.info("talkgroup names not downloaded: %s", e)
            return
        if len(tg) < 20:
            return
        tmp = self.tg_path + ".new"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({str(k): v for k, v in tg.items()}, f)
            os.replace(tmp, self.tg_path)
        except OSError as e:
            log.info("talkgroup names not saved: %s", e)
        self.tg = tg

    # ------------------------------------------------------------------ background loop
    def start(self):
        """Checks shortly after start-up and then every few hours; downloads only when the list is missing or old."""
        def loop():
            if self._stop.wait(FIRST_DELAY_S):
                return
            while True:
                try:
                    if self._auto() and self.due():
                        self.refresh(wait=True)
                except Exception as e:      # noqa: BLE001
                    log.warning("callsign list check failed: %s", e)
                if self._stop.wait(CHECK_EVERY_S):
                    return
        threading.Thread(target=loop, daemon=True, name="dmrdb-auto").start()

    def stop(self):
        self._stop.set()
        self._close()
