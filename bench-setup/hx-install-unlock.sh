#!/usr/bin/env bash
# Install the CMP 170HX unlock stack on a screening bench.
#
# THIS SCRIPT UNLOCKS NOTHING BY ITSELF. The unlock is cmpunlocker (GPL-2.0),
# https://github.com/amoghmunikote/cmpunlocker — not our work and not included
# here. All this does is run the existing pieces in the one order that works,
# with the guards we wish we had had the first time.
#
# WHY THIS EXISTS: cmpunlocker's install.sh aborts at Step 2/6 with
#   "No CMP 170HX GPU found (10de:20b0 / 10de:20c2 / 10de:2082)"
# so the unlock CANNOT be installed ahead of time on an empty bench. Everything
# that *can* be staged already is (the 610.43.03 installer and the cmpunlocker
# checkout). This script runs the rest, in the right order, once a card is in.
#
# The combination we validated:
#   nvidia-open 610.43.03  installed via .run with --dkms --kernel-module-type=open
#   then cmpunlocker install.sh, which strips the DKMS modules and installs its
#   own patched ones into /lib/modules/<kernel>/updates/cmpunlocker/
#
# AFTER THIS: **COLD BOOT** — a warm reboot does not reset the PCIe PHY, so the
# Gen2 retrain will not take. Power off, cut power for ~60 s, power on.
#
# Usage: sudo bash hx-install-unlock.sh [--yes]
set -uo pipefail

# Both are staged by hand ahead of time; see bench-setup/README.md.
RUN="${NVIDIA_RUN:-$HOME/NVIDIA-610.43.03.run}"
CMP="${CMPUNLOCKER:-$HOME/cmpunlocker}"
ASSUME_YES="${1:-}"

say(){ echo -e "$*"; }
die(){ echo "ABORT: $*" >&2; exit 1; }

say "=== CMP 170HX unlock install ==="

# ---- 1. the card must actually be present -------------------------------
say "[1/6] looking for a CMP 170HX on the bus"
LINES="$(lspci -nn 2>/dev/null | grep -iE '10de:20b0|10de:20c2|10de:2082' || true)"
if [ -z "$LINES" ]; then
    say ""
    say "  No CMP 170HX found. Cards seen:"
    lspci -nn | grep -iE 'vga|3d controller' | sed 's/^/    /'
    die "fit a 170HX first — the unlock cannot be installed without one"
fi
say "$LINES" | sed 's/^/    /'
N=$(echo "$LINES" | wc -l); say "    -> ${N} card(s)"

# ---- 2. preconditions ---------------------------------------------------
say "[2/6] preconditions"
[ "$(id -u)" -eq 0 ] || die "run with sudo"
[ -f "$RUN" ] || die "installer missing: $RUN"
[ -d "$CMP" ] || die "cmpunlocker checkout missing: $CMP"
[ -d "/lib/modules/$(uname -r)/build" ] || die "kernel headers missing for $(uname -r)"
SB="$(mokutil --sb-state 2>/dev/null | head -1)"
if [ -z "$SB" ] && ! command -v mokutil >/dev/null 2>&1; then
    # BIOS-only machines have no mokutil story; the .run installer will still
    # refuse to build unsigned modules under secure boot, so warn, don't die.
    say "    mokutil not present — verify Secure Boot is DISABLED in the BIOS"
    say "    (the unlocked kernel module is unsigned and will not load otherwise)"
elif [ -z "$SB" ]; then
    say "    secure boot state unreadable — verify it is DISABLED in the BIOS"
else
    case "$SB" in *disabled*) say "    secure boot: disabled ok" ;;
                           *) die "Secure Boot must be disabled (reads: ${SB})" ;; esac
fi
pgrep -x Xorg >/dev/null && die "Xorg is running — this box should be headless"
say "    kernel $(uname -r), headers present, no X"

if [ "$ASSUME_YES" != "--yes" ]; then
    say ""
    say "  This REPLACES the current driver ($(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null || echo none))"
    say "  with nvidia-open 610.43.03, then applies the cmpunlocker patches."
    read -r -p "  proceed? [y/N] " a; [ "$a" = "y" ] || die "cancelled"
fi

# ---- 3. remove the distro driver ---------------------------------------
say "[3/6] removing distro nvidia packages (the .run will not coexist)"
systemctl stop hx-dashboard 2>/dev/null
# NB: this pattern also removes nvidia-container-toolkit — re-install it
# afterwards if this bench runs GPU containers.
DEBIAN_FRONTEND=noninteractive apt-get -y purge '^nvidia-.*' '^libnvidia-.*' \
    >/tmp/unlock-purge.log 2>&1
if grep -q "nvidia-container-toolkit" /tmp/unlock-purge.log; then
    say "    NOTE: nvidia-container-toolkit was purged too — re-install it if"
    say "    this bench runs GPU containers (apt-get install nvidia-container-toolkit)"
fi
say "    purged $(grep -cE '^(Removing|Purging)' /tmp/unlock-purge.log) packages"

# ---- 4. nvidia-open 610.43.03 ------------------------------------------
say "[4/6] installing nvidia-open 610.43.03 (this takes a few minutes)"
sh "$RUN" --silent --dkms --kernel-module-type=open >/tmp/unlock-nvidia.log 2>&1
RC=$?
[ "$RC" -eq 0 ] || { tail -20 /tmp/unlock-nvidia.log; die ".run installer failed rc=$RC (see /tmp/unlock-nvidia.log)"; }
say "    installed: $(modinfo -F version nvidia 2>/dev/null || echo '?')  license: $(modinfo -F license nvidia 2>/dev/null || echo '?')"

# ---- 5. cmpunlocker -----------------------------------------------------
say "[5/6] applying cmpunlocker patches"
cd "$CMP" || die "cannot cd $CMP"
./install.sh 2>&1 | tee /tmp/unlock-cmp.log | tail -25
CMP_RC=${PIPESTATUS[0]}
if [ "${CMP_RC:-1}" -ne 0 ]; then
    tail -30 /tmp/unlock-cmp.log
    die "cmpunlocker install.sh exited rc=${CMP_RC} — NOT done, see /tmp/unlock-cmp.log"
fi
# target only real failure banners, not the word "die" in prose/paths
grep -qE "(^|[^a-z])(ABORT|FATAL|ERROR:|Traceback)" /tmp/unlock-cmp.log \
    && say "    NOTE: failure markers in /tmp/unlock-cmp.log — read it before booting"

# ---- 6. done ------------------------------------------------------------
say "[6/6] done"
say ""
say "  ***  COLD BOOT NOW  ***"
say "  Power off, cut power ~60 s, power on. A warm reboot does NOT reset the"
say "  PCIe PHY, so the Gen2 retrain will not take effect."
say ""
say "  After the cold boot, verify:"
say "    nvidia-smi --query-gpu=name,memory.total --format=csv   # expect 65536 MiB"
say "    vulkaninfo | grep -A1 'memoryHeaps\\[0\\]'                # expect 64.00 GiB"
say "    (if this bench set up the optional PCIe Gen2 link service: systemctl is-enabled gen2.service)"
say "  Then screen the card:  hx 15"
