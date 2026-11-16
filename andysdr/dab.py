"""DAB / DAB+ digital radio (UK Band III) through welle.io's `welle-cli`.

AndySDR keeps the receiver; welle-cli connects to AndySDR's local rtl_tcp-compatible IQ bridge
(see iqbridge.py) at 2.048 MS/s, decodes the ensemble and serves

    GET /mux.json        ensemble name, services (stations), now-playing text (DLS)
    GET /mp3/<sid>       the station's audio as an MP3 stream

AndySDR's own web server proxies those two URLs (with the password check) so the DAB tab - and any remote
screen on the network - can list stations and play them.
"""
import logging

log = logging.getLogger("andysdr.dab")

DAB_RATE = 2048000

# Band III channels (MHz).  Block -> centre frequency.
_CH = {
    "5A": 174.928, "5B": 176.640, "5C": 178.352, "5D": 180.064, "6A": 181.936, "6B": 183.648, "6C": 185.360, "6D": 187.072,
    "7A": 188.928, "7B": 190.640, "7C": 192.352, "7D": 194.064, "8A": 195.936, "8B": 197.648, "8C": 199.360, "8D": 201.072,
    "9A": 202.928, "9B": 204.640, "9C": 206.352, "9D": 208.064, "10A": 209.936, "10B": 211.648, "10C": 213.360, "10D": 215.072,
    "11A": 216.928, "11B": 218.640, "11C": 220.352, "11D": 222.064, "12A": 223.936, "12B": 225.648, "12C": 227.360, "12D": 229.072,
    "13A": 230.784, "13B": 232.496, "13C": 234.208, "13D": 235.776, "13E": 237.488, "13F": 239.200,
}
CHANNELS = {k: int(round(v * 1e6)) for k, v in _CH.items()}
# what the UK uses these for (national multiplexes; local ones differ by area)
UK_NOTES = {"12B": "BBC National DAB", "11D": "Digital One (commercial national)", "11B": "Sound Digital (national)",
            "10B": "local / regional", "10C": "local / regional", "10D": "local / regional", "11A": "local / regional",
            "12A": "local / regional", "12C": "local / regional", "12D": "local / regional"}


def channel_freq(ch):
    return CHANNELS.get(str(ch).upper())


def parse_mux(d):
    """welle-cli mux.json -> {"ensemble": str, "services": [{sid, name, dls, mp3}]}; tolerant of missing fields."""
    out = {"ensemble": "", "services": []}
    if not isinstance(d, dict):
        return out
    ens = d.get("ensemble") or {}
    lab = ens.get("label")
    out["ensemble"] = (lab.get("label") if isinstance(lab, dict) else lab) or ""
    out["ensemble"] = str(out["ensemble"]).strip()
    for s in d.get("services") or []:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("sid") or "").strip()
        if not sid:
            continue
        lab = s.get("label")
        name = (lab.get("label") if isinstance(lab, dict) else lab) or sid
        dls = s.get("dls")
        dls = (dls.get("label") if isinstance(dls, dict) else dls) or ""
        out["services"].append({"sid": sid, "name": str(name).strip(), "dls": str(dls).strip()})
    out["services"].sort(key=lambda x: x["name"].lower())
    return out
