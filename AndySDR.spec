# PyInstaller build recipe for AndySDR.exe  –  run:  pyinstaller --noconfirm AndySDR.spec
# Build inside a conda-forge environment that has SoapySDR + driver modules (see build_windows.bat).
import glob
import os
import sys

block_cipher = None
datas = [("andysdr/web", "web")]
binaries = []
hidden = ["scipy.signal", "scipy.special._cdflib", "webview", "SoapySDR", "andysdr.sdrplay"]

# ---- bundle SoapySDR runtime + every installed driver module (RTL-SDR, Airspy, HackRF, Remote ...)
prefix = os.environ.get("CONDA_PREFIX", sys.prefix)
lib_root = os.path.join(prefix, "Library") if os.name == "nt" else prefix
for moddir in glob.glob(os.path.join(lib_root, "lib", "SoapySDR", "modules*")):
    for f in glob.glob(os.path.join(moddir, "*")):
        binaries.append((f, os.path.join("soapy", "lib", "SoapySDR", os.path.basename(moddir))))
bindir = os.path.join(lib_root, "bin")
if os.name == "nt" and os.path.isdir(bindir):
    skip = ("mkl_", "Qt", "icu", "python3")
    for f in glob.glob(os.path.join(bindir, "*.dll")):
        if not os.path.basename(f).startswith(skip):
            binaries.append((f, os.path.join("soapy", "bin")))

a = Analysis(
    ["run_andysdr.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["matplotlib", "pandas", "IPython", "PyQt5", "PySide6", "notebook"],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="AndySDR",
    icon="andysdr/web/andysdr.ico",
    console=False,
    version=None,
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=False, name="AndySDR")
