"""Memory channels (10 banks x 100 channels, like a PRO-2045) and RadioReference CSV import."""
import csv
import io
import json
import os
import re
import threading

from .config import data_dir

BANKS = 10
CHANNELS_PER_BANK = 100

# RadioReference mode text -> (AndySDR mode, decoder id)
RR_MODES = {
    "FM": ("FM", ""), "FMN": ("NFM", ""), "NFM": ("NFM", ""), "AM": ("AM", ""),
    "WFM": ("WFM", ""), "USB": ("USB", ""), "LSB": ("LSB", ""), "CW": ("CW", ""),
    "DMR": ("DIG", "dsd"), "DMRN": ("DIG", "dsd"), "TRBO": ("DIG", "dsd"),
    "P25": ("DIG", "dsd"), "APCO25": ("DIG", "dsd"), "P25P2": ("DIG", "dsd"),
    "NXDN": ("DIG", "dsd"), "NXDN48": ("DIG", "dsd"), "NXDN96": ("DIG", "dsd"),
    "DPMR": ("DIG", "dsd"), "DSTAR": ("DIG", "dsd"), "D-STAR": ("DIG", "dsd"),
    "YSF": ("DIG", "dsd"), "C4FM": ("DIG", "dsd"), "M17": ("DIG", "dsd"),
    "TETRA": ("DIG25", "tetra"), "POCSAG": ("DIG", "pager"), "FLEX": ("DIG", "pager"),
    "PAGER": ("DIG", "pager"), "DATA": ("DIG", ""), "TELEMETRY": ("DIG", ""),
    "FMN/DMR": ("DIG", "dsd"), "FM/DMR": ("DIG", "dsd"), "WEFAX": ("USB", "wefax"),
    "FAX": ("USB", "wefax"),
}


def _norm(h):
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def _find_col(headers, *names, contains=None):
    nh = [_norm(h) for h in headers]
    for name in names:
        n = _norm(name)
        if n in nh:
            return nh.index(n)
    if contains:
        for i, h in enumerate(nh):
            if any(c in h for c in contains):
                return i
    return None


def parse_frequency(text):
    """'145.5000' (MHz), '145500000', '145.500 MHz', '446.00625' -> Hz. Returns None if not a freq."""
    if text is None:
        return None
    t = str(text).strip().upper().replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(GHZ|MHZ|KHZ|HZ)?", t)
    if not m:
        return None
    v = float(m.group(1))
    unit = m.group(2)
    if unit == "GHZ":
        v *= 1e9
    elif unit == "MHZ":
        v *= 1e6
    elif unit == "KHZ":
        v *= 1e3
    elif unit == "HZ":
        pass
    else:
        # bare number: guess
        if v < 30000:          # looks like MHz (RadioReference lists MHz)
            v *= 1e6
        elif v < 3e7 and "." not in m.group(1):   # kHz
            v *= 1e3
    if v < 9e3 or v > 7e9:
        return None
    return int(round(v))


def map_mode(text):
    t = (text or "").strip().upper().replace(" ", "")
    if t in RR_MODES:
        return RR_MODES[t]
    for k, v in RR_MODES.items():
        if k in t:
            return v
    return ("NFM", "")


def parse_radioreference_csv(raw_text):
    """Parse a RadioReference (UK or any country) export / copied table.

    Works with the standard RR CSV columns (Frequency Output / Frequency / Alpha Tag /
    Description / Mode / Tag / PL Tone / Tone) and with tab-separated tables copied straight
    from the radioreference.com web pages. Trunked talkgroup exports (Decimal/Alpha Tag)
    are returned separately as talkgroups.
    """
    text = raw_text.lstrip("﻿")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel_tab if sample.count("\t") > sample.count(",") else csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return {"channels": [], "talkgroups": [], "skipped": 0}

    # find header row (first row containing a "freq" or "decimal" column)
    hdr_i = 0
    for i, r in enumerate(rows[:10]):
        joined = " ".join(_norm(c) for c in r)
        if "freq" in joined or "decimal" in joined:
            hdr_i = i
            break
    headers = rows[hdr_i]
    body = rows[hdr_i + 1:]

    c_freq = _find_col(headers, "Frequency Output", "Frequency", "Freq", "Output", contains=("freq",))
    c_dec = _find_col(headers, "Decimal", "DEC", "TGID")
    c_alpha = _find_col(headers, "Alpha Tag", "AlphaTag", "Alpha", "Name", "Callsign", "Licensee")
    c_desc = _find_col(headers, "Description", "Desc", "Agency", "Agency/Category", "Location",
                       contains=("desc",))
    c_mode = _find_col(headers, "Mode", "Type", "Modulation", contains=("mode",))
    c_tag = _find_col(headers, "Tag", "Service Tag", "Category", "Group")
    c_tone = _find_col(headers, "PL Tone", "Tone", "CTCSS", "CC", "Color Code", "NAC", "RAN",
                       contains=("tone",))

    def cell(r, c):
        return r[c].strip() if c is not None and c < len(r) else ""

    channels, talkgroups, skipped = [], [], 0
    if c_freq is None and c_dec is not None:
        for r in body:
            dec = cell(r, c_dec)
            if not dec.isdigit():
                skipped += 1
                continue
            talkgroups.append({"tgid": int(dec), "name": cell(r, c_alpha) or cell(r, c_desc),
                               "description": cell(r, c_desc), "mode": cell(r, c_mode),
                               "tag": cell(r, c_tag)})
        return {"channels": [], "talkgroups": talkgroups, "skipped": skipped}

    for r in body:
        f = parse_frequency(cell(r, c_freq if c_freq is not None else 0))
        if not f:
            skipped += 1
            continue
        mode, decoder = map_mode(cell(r, c_mode))
        name = cell(r, c_alpha) or cell(r, c_desc)[:24]
        channels.append({
            "freq": f, "mode": mode, "decoder": decoder,
            "name": name[:32], "description": cell(r, c_desc)[:120],
            "tag": cell(r, c_tag), "tone": cell(r, c_tone),
            "rr_mode": cell(r, c_mode),
        })
    return {"channels": channels, "talkgroups": talkgroups, "skipped": skipped}


class MemoryBank:
    def __init__(self):
        self.path = os.path.join(data_dir(), "memory.json")
        self.lock = threading.Lock()
        self.data = {"banks": {str(b): {"name": f"BANK {b}", "enabled": True, "channels": {}}
                               for b in range(BANKS)},
                     "talkgroups": []}
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self.data.update(loaded)
            except Exception:
                pass
        if not os.path.exists(self.path):
            self._seed()
        self.save()

    def _seed(self):
        demo = [
            (145500000, "NFM", "2m Calling", "Amateur"),
            (145550000, "NFM", "2m Simplex S22", "Amateur"),
            (446006250, "NFM", "PMR446 Ch1", "PMR"),
            (446093750, "NFM", "PMR446 Ch8", "PMR"),
            (156800000, "NFM", "Marine Ch16", "Marine"),
            (121500000, "AM", "Air Distress", "Aviation"),
            (118700000, "AM", "Airband sample", "Aviation"),
            (98500000, "WFM", "FM broadcast", "Broadcast"),
            (153350000, "DIG", "Pager sample", "Paging"),
            (8040000, "USB", "WEFAX Northwood", "Weather"),
            (137900000, "DIG25", "Meteor-M LRPT", "Weather Sat"),
        ]
        ch = {}
        for i, (f, m, n, t) in enumerate(demo):
            dec = "wefax" if "WEFAX" in n else ("pager" if "Pager" in n else
                                                ("lrpt" if "Meteor" in n else ""))
            ch[str(i)] = {"freq": f, "mode": m, "name": n, "tag": t, "decoder": dec,
                          "lockout": False, "delay": True, "description": "", "tone": ""}
        self.data["banks"]["0"]["channels"] = ch
        self.data["banks"]["0"]["name"] = "STARTER"

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)

    def all(self):
        return self.data

    def store(self, bank, ch, entry):
        bank, ch = str(int(bank)), int(ch)
        e = {"freq": int(entry["freq"]), "mode": entry.get("mode", "NFM"),
             "name": entry.get("name", "")[:32], "tag": entry.get("tag", ""),
             "decoder": entry.get("decoder", ""), "lockout": bool(entry.get("lockout", False)),
             "delay": bool(entry.get("delay", True)), "description": entry.get("description", ""),
             "tone": entry.get("tone", "")}
        self.data["banks"][bank]["channels"][str(ch)] = e
        self.save()
        return e

    def delete(self, bank, ch):
        self.data["banks"][str(int(bank))]["channels"].pop(str(int(ch)), None)
        self.save()

    def update(self, bank, ch, fields):
        c = self.data["banks"][str(int(bank))]["channels"].get(str(int(ch)))
        if c:
            for k in ("lockout", "delay", "name", "mode", "decoder", "tag"):
                if k in fields:
                    c[k] = fields[k]
            self.save()
        return c

    def set_bank(self, bank, fields):
        b = self.data["banks"][str(int(bank))]
        if "name" in fields:
            b["name"] = str(fields["name"])[:16]
        if "enabled" in fields:
            b["enabled"] = bool(fields["enabled"])
        self.save()

    def clear_bank(self, bank):
        self.data["banks"][str(int(bank))]["channels"] = {}
        self.save()

    def free_slot(self, bank, start=0):
        used = self.data["banks"][str(int(bank))]["channels"]
        for i in range(start, CHANNELS_PER_BANK):
            if str(i) not in used:
                return i
        return None

    def import_channels(self, bank, channels, replace=False, bank_name=None):
        bank = str(int(bank))
        if replace:
            self.data["banks"][bank]["channels"] = {}
        if bank_name:
            self.data["banks"][bank]["name"] = bank_name[:16]
        added, overflow = 0, 0
        b = int(bank)
        for c in channels:
            slot = self.free_slot(b)
            if slot is None:
                # spill into next banks with free space
                nb = next((k for k in range(BANKS) if self.free_slot(k) is not None), None)
                if nb is None:
                    overflow += 1
                    continue
                b = nb
                slot = self.free_slot(b)
            entry = dict(c)
            entry.setdefault("lockout", False)
            entry.setdefault("delay", True)
            self.data["banks"][str(b)]["channels"][str(slot)] = entry
            added += 1
        self.save()
        return added, overflow

    def import_talkgroups(self, tgs):
        known = {t["tgid"] for t in self.data.get("talkgroups", [])}
        for t in tgs:
            if t["tgid"] not in known:
                self.data.setdefault("talkgroups", []).append(t)
        self.save()

    def scan_list(self):
        """Channels to scan, in bank/channel order, skipping lockouts and disabled banks."""
        out = []
        for b in range(BANKS):
            bank = self.data["banks"][str(b)]
            if not bank.get("enabled", True):
                continue
            for k in sorted(bank["channels"], key=lambda s: int(s)):
                c = bank["channels"][k]
                if not c.get("lockout"):
                    out.append((b, int(k), c))
        return out

    def export_csv(self):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Bank", "Channel", "Frequency", "Mode", "Alpha Tag", "Description", "Tag",
                    "Tone", "Decoder", "Lockout"])
        for b in range(BANKS):
            for k, c in sorted(self.data["banks"][str(b)]["channels"].items(), key=lambda kv: int(kv[0])):
                w.writerow([b, k, f"{c['freq'] / 1e6:.5f}", c.get("mode"), c.get("name"),
                            c.get("description", ""), c.get("tag", ""), c.get("tone", ""),
                            c.get("decoder", ""), "Y" if c.get("lockout") else ""])
        return buf.getvalue()
