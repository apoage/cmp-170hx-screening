#!/usr/bin/env bash
# Set up a CMP 170HX screening bench from this checkout.
#
# Installs the two dependencies (vulkan-tools, memtest_vulkan), creates the
# results directory, and offers to install the dashboard as a systemd service.
# It does NOT install the GPU driver or the unlock — see bench-setup/README.md,
# which has to happen first and needs a card already fitted.
#
# Usage: bash install.sh [--service]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="${HX_BASE:-$HOME/vramscreen}"
MTV_VERSION="${MTV_VERSION:-0.5.0}"
MTV_URL="https://github.com/GpuZelenograd/memtest_vulkan/releases/download/v${MTV_VERSION}/memtest_vulkan-v${MTV_VERSION}_DesktopLinux_X86_64.tar.xz"

say(){ echo "==> $*"; }
die(){ echo "ABORT: $*" >&2; exit 1; }

say "results directory: $BASE/results"
mkdir -p "$BASE/results" || die "cannot create $BASE/results"

# ---- 1. vulkan-tools ------------------------------------------------------
# vulkaninfo is what lets a report state the 64 GiB heap through a NON-NVIDIA
# API. Without it the screen still runs, but section 3 of every report reads
# "not measured" and loses its single best piece of independent evidence.
if command -v vulkaninfo >/dev/null 2>&1; then
    say "vulkaninfo present"
else
    say "installing vulkan-tools (needs sudo)"
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update -qq && sudo apt-get install -y vulkan-tools \
            || say "WARNING: install failed — reports will omit the Vulkan heap check"
    else
        say "WARNING: not an apt system. Install vulkan-tools yourself."
    fi
fi

# ---- 2. memtest_vulkan ----------------------------------------------------
# Upstream: GpuZelenograd/memtest_vulkan (note: NOT GpuZoo, a different project).
# Vendored nowhere in this repo — it is somebody else's binary and you should
# fetch it from its own release page so you can verify what you are running.
if [ -x "$BASE/memtest_vulkan" ]; then
    say "memtest_vulkan already at $BASE/memtest_vulkan"
else
    say "fetching memtest_vulkan v${MTV_VERSION}"
    TMP="$(mktemp -d)"
    if curl -fsSL "$MTV_URL" -o "$TMP/mtv.tar.xz"; then
        tar -xJf "$TMP/mtv.tar.xz" -C "$TMP" \
            && find "$TMP" -name memtest_vulkan -type f -exec cp {} "$BASE/memtest_vulkan" \; \
            && chmod +x "$BASE/memtest_vulkan" \
            && say "installed $BASE/memtest_vulkan"
    else
        say "WARNING: download failed. Fetch it by hand from"
        say "  https://github.com/GpuZelenograd/memtest_vulkan/releases"
        say "and drop the binary at $BASE/memtest_vulkan"
    fi
    rm -rf "$TMP"
fi

# ---- 3. sanity ------------------------------------------------------------
say "checking the bench"
command -v nvidia-smi >/dev/null 2>&1 \
    || say "WARNING: no nvidia-smi — install the driver first (bench-setup/README.md)"
if command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi --query-gpu=index,name,serial,memory.total --format=csv | sed 's/^/    /'
    nvidia-smi --query-gpu=name --format=csv,noheader | grep -q "CMP 170HX" \
        || say "NOTE: no CMP 170HX visible. If one is fitted, it is not unlocked yet."
fi

# ---- 3b. card dossier selftest ---------------------------------------------
# cardreport.py is stdlib-only; this validates its analytics against a
# reference card without needing any GPU present.
if python3 "$HERE/bin/cardreport.py" --selftest >/dev/null 2>&1; then
    say "cardreport selftest ok (dossier analytics verified offline)"
else
    say "WARNING: cardreport selftest FAILED — card dossier output would be suspect"
fi

# ---- 4. dashboard service (optional) --------------------------------------
if [ "${1:-}" = "--service" ]; then
    say "installing hx-dashboard.service on port ${HX_PORT:-8097}"
    sed -e "s|@USER@|$USER|g" -e "s|@BASE@|$BASE|g" -e "s|@HERE@|$HERE|g" \
        -e "s|@PORT@|${HX_PORT:-8097}|g" \
        "$HERE/bench-setup/hx-dashboard.service.in" | sudo tee /etc/systemd/system/hx-dashboard.service >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable --now hx-dashboard.service
    sudo systemctl --no-pager status hx-dashboard.service | head -5
fi

say "done. Screen a card with:  bash $HERE/bin/hx-vram-screen.sh 15"
say "      Card dossier alone:  sudo python3 $HERE/bin/cardreport.py"
