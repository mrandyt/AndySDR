"""Download the free decoder programs AndySDR uses (DSD-FME, multimon-ng, SatDump).

Used in two places:
  * the Windows build (GitHub Actions / build_windows.bat) – bundles them next to AndySDR.exe:
        python -m andysdr.fetchdecoders dist/AndySDR/decoders
  * the DECODERS tab – "Download missing decoders" fetches anything not yet installed into
    %APPDATA%\\AndySDR\\decoders (no admin rights needed).

Each program is taken from the newest GitHub release that has a Windows build. All three are
free software (GPL); their licence files stay in their folders and LICENSES.txt lists the sources.
"""
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

UA = {"User-Agent": "AndySDR-decoder-fetch", "Accept": "application/vnd.github+json"}

# name -> where to look and which release file is the Windows build
PROGRAMS = {
    "dsd-fme": {
        "exe": "dsd-fme",
        "repos": ["lwvmobile/dsd-fme"],
        # DSD-FME's Windows releases are Cygwin builds: a full zip (DLLs, pulseaudio, .bat
        # examples) plus, in newer releases, a bare updated dsd-fme.exe dropped on top.
        "asset": r"\.(zip|7z)$",
        "avoid": r"(source|src|linux|arm|aarch64|mac|osx|\.deb|appimage|\.tar)",
        "overlay": r"^dsd-fme\.exe$",
        "licence": "GPL-2.0 – https://github.com/lwvmobile/dsd-fme",
    },
    "multimon-ng": {
        "exe": "multimon-ng",
        "repos": ["EliasOenal/multimon-ng"],
        "asset": r"(win64|x64|w64|amd64).*\.(zip|7z)$",
        "avoid": r"(source|src|linux|arm|aarch64|mac|osx|\.deb)",
        "licence": "GPL-2.0 – https://github.com/EliasOenal/multimon-ng",
    },
    "satdump": {
        "exe": "satdump",
        "repos": ["SatDump/SatDump"],
        "asset": r"windows.*x64.*portable.*\.zip$|windows.*portable.*\.zip$",
        "avoid": r"(arm|aarch64|macos|linux|\.deb|appimage|android|apk|cli_only)",
        "licence": "GPL-3.0 – https://github.com/SatDump/SatDump",
    },
}


def _get(url, token=None):
    h = dict(UA)
    if token:
        h["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=120) as r:
        return r.read()


def find_release_asset(prog, token=None, log=print):
    """Return (tag, asset_name, download_url, overlay_url) of the newest Windows build, or None."""
    p = PROGRAMS[prog]
    want, avoid = re.compile(p["asset"], re.I), re.compile(p["avoid"], re.I)
    for repo in p["repos"]:
        try:
            rels = json.loads(_get(f"https://api.github.com/repos/{repo}/releases?per_page=30", token))
        except Exception as e:  # noqa: BLE001
            log(f"{prog}: could not read releases of {repo}: {e}")
            continue
        # stable releases first, then pre-releases / nightlies
        for stable_pass in (True, False):
            for rel in rels:
                if rel.get("draft") or bool(rel.get("prerelease")) == stable_pass:
                    continue
                assets = rel.get("assets", [])
                over = None
                if p.get("overlay"):
                    ov = re.compile(p["overlay"], re.I)
                    over = next((a["browser_download_url"] for a in assets if ov.search(a.get("name", ""))), None)
                for a in assets:
                    n = a.get("name", "")
                    if want.search(n) and not avoid.search(n):
                        return rel.get("tag_name", "?"), n, a["browser_download_url"], over
    return None


def _extract(blob, name, dest):
    os.makedirs(dest, exist_ok=True)
    if name.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            z.extractall(dest)
        return
    # .7z etc. – Windows 10/11 ships bsdtar (tar.exe) which reads 7z; 7-Zip is used if present
    with tempfile.TemporaryDirectory() as td:
        arc = os.path.join(td, name)
        with open(arc, "wb") as f:
            f.write(blob)
        for cmd in (["7z", "x", "-y", f"-o{dest}", arc], ["tar", "-xf", arc, "-C", dest]):
            if shutil.which(cmd[0]):
                if subprocess.run(cmd, capture_output=True).returncode == 0:
                    return
        raise RuntimeError(f"cannot unpack {name} (needs 7-Zip or Windows tar)")


def installed(prog, dest):
    hits = glob.glob(os.path.join(dest, "**", PROGRAMS[prog]["exe"] + ".exe"), recursive=True)
    # the top-most copy is the main program (DSD-FME zips also hold patched variants deeper down)
    hits.sort(key=lambda h: (h.count(os.sep), len(h)))
    return hits[0] if hits else None


def fetch(prog, dest, token=None, log=print, force=False):
    """Download + unpack one program into dest/<prog>/. Returns path of the .exe or None."""
    sub = os.path.join(dest, prog)
    if not force:
        have = installed(prog, dest)
        if have:
            log(f"{prog}: already installed ({have})")
            return have
    hit = find_release_asset(prog, token, log)
    if not hit:
        log(f"{prog}: no Windows build found on GitHub releases")
        return None
    tag, name, url, overlay = hit
    log(f"{prog}: downloading {name} ({tag})...")
    try:
        blob = _get(url, token)
        if os.path.isdir(sub):
            shutil.rmtree(sub, ignore_errors=True)
        _extract(blob, name, sub)
    except Exception as e:  # noqa: BLE001
        log(f"{prog}: download/unpack failed: {e}")
        return None
    path = installed(prog, sub)
    if path and overlay:
        try:
            newexe = _get(overlay, token)
            shutil.copy2(path, path[:-4] + "-full-release.exe")
            with open(path, "wb") as f:
                f.write(newexe)
            log(f"{prog}: applied the updated {os.path.basename(path)} from the same release")
        except Exception as e:  # noqa: BLE001
            log(f"{prog}: updated exe not applied ({e}) - using the one from the zip")
    if path:
        with open(os.path.join(sub, "ANDYSDR_SOURCE.txt"), "w", encoding="utf-8") as f:
            f.write(f"{prog} {tag}\nfile: {name}\nfrom: {url}\nlicence: {PROGRAMS[prog]['licence']}\n")
        log(f"{prog}: installed {tag} -> {path}")
    else:
        log(f"{prog}: unpacked {name} but no {PROGRAMS[prog]['exe']}.exe inside")
    return path


def write_licences(dest):
    os.makedirs(dest, exist_ok=True)
    with open(os.path.join(dest, "LICENSES.txt"), "w", encoding="utf-8") as f:
        f.write("Decoder programs bundled with AndySDR\n=====================================\n\n"
                "These are separate free programs, run by AndySDR as external processes and\n"
                "distributed unmodified. Their source code is available from:\n\n")
        for k, p in PROGRAMS.items():
            f.write(f"  {k:<12} {p['licence']}\n")
        f.write("\nEach folder's ANDYSDR_SOURCE.txt records the exact release that was included.\n")


def fetch_all(dest, token=None, log=print, force=False):
    token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    res = {k: fetch(k, dest, token, log, force) for k in PROGRAMS}
    write_licences(dest)
    return res


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Download AndySDR's decoder programs")
    ap.add_argument("dest", nargs="?", default=os.path.join("dist", "AndySDR", "decoders"))
    ap.add_argument("--force", action="store_true", help="re-download even if present")
    ap.add_argument("--require", default="", help="comma list that must succeed, e.g. satdump,dsd-fme")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    res = fetch_all(a.dest, force=a.force)
    print("\nSummary:")
    for k, v in res.items():
        print(f"  {k:<12} {'OK  ' + v if v else 'MISSING'}")
    missing = [k for k in filter(None, a.require.split(",")) if not res.get(k)]
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
