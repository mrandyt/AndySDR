#!/usr/bin/env bash
# Builds tetra-rx (osmo-tetra, TETRA MAC/PHY decoder) without the whole of libosmocore.
#   build.sh <osmo-tetra-sq5bpf checkout> <libosmocore checkout> <output dir> [compiler, default gcc]
# For Windows pass x86_64-w64-mingw32-gcc (or run inside MSYS2 MINGW64 with plain gcc).
# Only the few libosmocore files tetra-rx needs are compiled (convolutional decoder, bit helpers, msgb,
# and its tiny "pseudotalloc"), so no libosmocore build system, sockets or logging library is required.
set -euo pipefail
OT=$(cd "$1" && pwd); OC=$(cd "$2" && pwd); OUT=$3; CC=${4:-gcc}
HERE=$(cd "$(dirname "$0")" && pwd)
W=$(mktemp -d); mkdir -p "$OUT"; OUT=$(cd "$OUT" && pwd)
cp -r "$OT/src" "$W/src"
cd "$W/src"
WIN=0; case "$($CC -dumpmachine)" in *mingw*) WIN=1;; esac

# tetra-rx reads its symbol file by name: let "-" mean standard input so AndySDR can pipe symbols to it
sed -i 's|fd = open(argv\[optind\], O_RDONLY);|fd = (argv[optind][0] == 45 \&\& !argv[optind][1]) ? 0 : open(argv[optind], O_RDONLY);|' tetra-rx.c
grep -q 'argv\[optind\]\[0\] == 45' tetra-rx.c || { echo "tetra-rx.c changed upstream - stdin patch did not apply" >&2; exit 1; }

CFLAGS="-O2 -fcommon -w -D_GNU_SOURCE -I. -I$HERE/ocfg -I$OC/include -I$OC/src/pseudotalloc"
[ $WIN = 1 ] && CFLAGS="-I$HERE/shim -include $HERE/shim/win_compat.h $CFLAGS"
mkdir -p "$W/obj"
OBJS=()
cc1() { local o="$W/obj/$(echo "$1" | tr / _).o"; $CC $CFLAGS -c "$1" -o "$o"; OBJS+=("$o"); }

for f in tetra-rx phy/tetra_burst_sync phy/tetra_burst lower_mac/tetra_conv_enc lower_mac/tch_reordering tetra_tdma \
         lower_mac/tetra_scramb lower_mac/tetra_rm3014 lower_mac/tetra_interleave lower_mac/crc_simple tetra_common \
         lower_mac/viterbi lower_mac/viterbi_cch lower_mac/viterbi_tch lower_mac/tetra_lower_mac tetra_upper_mac \
         tetra_mac_pdu tetra_llc_pdu tetra_mle_pdu tetra_mm_pdu tetra_cmce_pdu tetra_sndcp_pdu tetra_sds; do
  cc1 "$f.c"
done
for f in conv conv_acc conv_acc_generic bits utils msgb bitvec; do
  $CC $CFLAGS -I"$OC/src/core" -c "$OC/src/core/$f.c" -o "$W/obj/oc_$f.o"; OBJS+=("$W/obj/oc_$f.o")
done
$CC $CFLAGS -c "$OC/src/pseudotalloc/pseudotalloc.c" -o "$W/obj/pt.o"; OBJS+=("$W/obj/pt.o")
$CC $CFLAGS -c "$HERE/tetra_stubs.c" -o "$W/obj/stubs.o"; OBJS+=("$W/obj/stubs.o")
cc1 float_to_bits.c 2>/dev/null || true
LIBS="-lm"; [ $WIN = 1 ] && LIBS="-static -lws2_32 -lm"
EXE=tetra-rx; [ $WIN = 1 ] && EXE=tetra-rx.exe
# float_to_bits has its own main(): keep it out of the tetra-rx link
LINK=(); for o in "${OBJS[@]}"; do case "$o" in *float_to_bits*) ;; *) LINK+=("$o");; esac; done
$CC -o "$OUT/$EXE" "${LINK[@]}" $LIBS
echo "built $OUT/$EXE"
