AndySDR decoders folder
=======================

The AndySDR download already contains these free programs (one sub-folder each):

  dsd-fme\         DMR, P25 Phase 1/2, NXDN, dPMR, YSF, M17, ProVoice, X2-TDMA
                   https://github.com/lwvmobile/dsd-fme  (Cygwin Windows build + its PulseAudio,
                   which AndySDR starts automatically so decoded voice is heard)

  multimon-ng\     POCSAG 512/1200/2400, FLEX pagers, APRS, DTMF, ZVEI, EAS
                   https://github.com/EliasOenal/multimon-ng  (official Windows 64-bit build)

  satdump\         Meteor-M N2-3 / N2-4 LRPT weather satellite images
                   https://www.satdump.org  (Windows x64 portable)

WEFAX (HF weather fax) and SSTV are built into AndySDR - nothing to install.

If one is missing (the DECODERS tab shows a red cross), press "Download missing decoders" in
that tab: AndySDR fetches it from GitHub into %APPDATA%\AndySDR\decoders. You can also unzip
your own copy here - any sub-folder is fine, AndySDR finds the .exe.

All three are GPL free software, distributed unmodified; see LICENSES.txt and each folder's
ANDYSDR_SOURCE.txt for the exact version and where its source code is.
