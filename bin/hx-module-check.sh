#!/usr/bin/env bash
# Module provenance check: refuse a kernel module carrying the marker string
# of 0010-sec2-hs-fusegate-probe — a LOCAL experimental patch from our own
# bench work (never part of cmpunlocker; see CHANGELOG 1.6.1) that loops WPR2
# base/size writes + Booter re-exec and can corrupt host RAM. No outside user
# can have it; this guard exists so our own benches can never again boot a
# module built with it.
#
# The marker 'SEC2_DEBUG: HS ' is unique to that patch — upstream builds log
# plain SEC2_DEBUG lines (e.g. SEC2_DEBUG_HEAP), which this pattern does not
# match. DKMS often stores modules compressed (.ko.zst/.ko.xz/.ko.gz):
# unpack before grepping. Unreadable module => "not checked", never silence.
#
# Exit: 0 clean (or not-checked), 3 marker found.
# Optional $1: explicit module path (used by CI with fake modules).
set -u
if [ -n "${1:-}" ]; then
    MODPATH="$1"
else
    MODPATH="$(modinfo -n nvidia 2>/dev/null)"
    if [ -z "$MODPATH" ] || [ ! -r "$MODPATH" ]; then
        echo "module provenance: not checked (no readable module file)"
        exit 0
    fi
fi
case "$MODPATH" in
    *.zst) CAT="zstdcat" ;;
    *.xz)  CAT="xzcat" ;;
    *.gz)  CAT="gzip -dc" ;;
    *)     CAT="cat" ;;
esac
if ! $CAT "$MODPATH" >/dev/null 2>&1; then
    echo "module provenance: not checked (unreadable or no decompressor)"
    exit 0
fi
# NB: grep -c prints 0 but exits 1 on zero matches — the '|| :' keeps a
# CLEAN module from reading as a failed check.
FUSEGATE="$($CAT "$MODPATH" 2>/dev/null | grep -a -c 'SEC2_DEBUG: HS ' || :)"
if [ "${FUSEGATE:-0}" -gt 0 ]; then
    echo "ABORT: the nvidia module carries the HS fusegate probe marker" >&2
    echo "  (local patch 0010-sec2-hs-fusegate-probe — never part of" >&2
    echo "  cmpunlocker). It can corrupt host RAM. Rebuild without it." >&2
    exit 3
fi
echo "module provenance: ok (no fusegate marker)"
LOADED="$(cat /sys/module/nvidia/srcversion 2>/dev/null)"
if [ -n "$LOADED" ] && [ "$LOADED" != "$(modinfo -F srcversion nvidia 2>/dev/null)" ]; then
    echo "NOTE: loaded module srcversion differs from the file on disk —"
    echo "  the provenance check ran against the file, not the loaded module"
fi
