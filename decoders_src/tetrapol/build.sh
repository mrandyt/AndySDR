#!/usr/bin/env bash
# Builds tetrapol_dump (Tetrapol-kit, GPL-2.0, https://github.com/jswo/tetrapol-kit) - the TETRAPOL frame decoder
# AndySDR pipes its demodulated bits into.
#   build.sh <tetrapol-kit checkout> <output dir> [compiler, default gcc]
# Needs glib-2.0 (pkg-config).  Windows: run inside MSYS2 MINGW64 with mingw-w64-x86_64-{gcc,glib2,pkgconf}.
# Tetrapol-kit's own CMake build also wants cmocka (only for its unit tests) and json-c (only for the separate
# tetrapol_build tool), so it is compiled here directly: the library sources plus apps/tetrapol_dump.c.
set -euo pipefail
TK=$(cd "$1" && pwd); OUT=$2; CC=${3:-gcc}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT"; OUT=$(cd "$OUT" && pwd)
WIN=0; case "$($CC -dumpmachine)" in *mingw*) WIN=1;; esac
CFLAGS="-std=gnu11 -D_XOPEN_SOURCE=700 -D_DEFAULT_SOURCE -O2 -w -I$TK/lib $(pkg-config --cflags glib-2.0)"
LIBS="$(pkg-config --libs glib-2.0)"
EXE=tetrapol_dump
if [ $WIN = 1 ]; then
  CFLAGS="-I$HERE/shim -include $HERE/shim/win_compat.h $CFLAGS"
  EXE=tetrapol_dump.exe
fi
SRC=()
for f in "$TK"/lib/*.c; do case "$(basename "$f")" in test_*) ;; *) SRC+=("$f");; esac; done
$CC $CFLAGS -o "$OUT/$EXE" "$TK/apps/tetrapol_dump.c" "${SRC[@]}" $LIBS
echo "built $OUT/$EXE"
