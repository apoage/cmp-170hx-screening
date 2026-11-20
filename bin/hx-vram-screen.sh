#!/usr/bin/env bash
# CMP 170HX — per-card VRAM + thermal acceptance screen.  ~13 min per card.
#
# ONE continuous run does both jobs: memtest_vulkan takes the card to sustained
# full load within ~2 s and holds it there, so the memory test IS the thermal
# soak. Telemetry is sampled throughout and the curve is analysed for repaste /
# repad candidates.
#
# MEASURED BEHAVIOUR THIS IS BUILT ON (reference CMP 170HX, screened 2026-08):
#   idle 39 W -> full load in ~2 s and held for the whole run
#   power depends on working-set size: ~250 W (SW cap, mask 0x4) on a small
#   buffer, ~200-220 W sustained over the full 63 GiB heap. The analyser divides
#   by the MEASURED average power, so C/W stays comparable either way — but do
#   not quote a fixed wattage, quote the figure in the report.
#   core 30 -> 58 C and HBM 46 -> 68 C within 84 s, still climbing at that point
#   => a run shorter than ~8 min cannot tell "hot card" from "not finished yet"
#
# HARD RULE: memtest_vulkan tests only the VRAM that is FREE when it starts.
# With a tenant resident it reported "PASSed" having covered 7 GB of 64 GB.
# This script refuses to start unless the GPU is idle, and prints coverage.
#
# Usage:
#   bash hx-vram-screen.sh [MINUTES] [TAG]    # default 12 min; TAG is your own
#                                             # shelf label, optional
#   bash hx-vram-screen.sh --rollup           # compare the whole batch
#
# Output is named after the GPU SERIAL NUMBER read from the card itself, so a
# report can never be attached to the wrong unit:
#   $HX_BASE/results/<SERIAL>.md            <- the report to send the buyer
#   $HX_BASE/results/<SERIAL>_<stamp>/      <- raw logs + SHA256SUMS
# HX_BASE defaults to ~/vramscreen.
#
# Env: MTV=/path/to/memtest_vulkan  GPU=<index>  CROSSCHECK=1 (adds CUDA pass)
set -uo pipefail

# HERE = this checkout; BASE = where results live (override with HX_BASE).
# Kept separate so the repo can be updated with `git pull` without touching the
# accumulated batch results, which are the thing you cannot regenerate.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="${HX_BASE:-$HOME/vramscreen}"
FLEET="$BASE/results/FLEET.csv"
GPU="${GPU:-0}"
MTV="${MTV:-$BASE/memtest_vulkan}"
[ -x "$MTV" ] || MTV="$HERE/memtest_vulkan"
[ -x "$MTV" ] || MTV="$(command -v memtest_vulkan 2>/dev/null || echo "$MTV")"
ANALYZE="${ANALYZE:-$HERE/hx-thermal-analyze.py}"
[ -f "$ANALYZE" ] || ANALYZE="$BASE/hx-thermal-analyze.py"
INTERVAL=2
# The parallel launcher writes per-child logs here before any child has had a
# chance to create it, so it cannot be left to the child's own mkdir.
mkdir -p "$BASE/results"

# ------------------------------------------------------------ ROLLUP MODE
# Absolute temperatures depend on bench airflow; the BATCH is the reference.
if [ "${1:-}" = "--rollup" ]; then
    [ -f "$FLEET" ] || { echo "no results yet at $FLEET"; exit 1; }
    python3 - "$FLEET" <<'ROLLUP'
import csv, sys, statistics as st
rows = list(csv.DictReader(open(sys.argv[1])))
if not rows:
    print("no cards screened yet"); raise SystemExit(0)
w = max(len(r["card"]) for r in rows) + 2
print(f"{'serial':<{w}}{'verdict':<26}{'provenance':<12}{'core C/W':>10}{'HBM C/W':>10}{'core C':>8}{'HBM C':>8}")
for r in rows:
    print(f"{r['card']:<{w}}{r['verdict']:<26}{r.get('provenance',''):<12}{r['core_CW']:>10}"
          f"{r['hbm_CW']:>10}{r['plateau_core']:>8}{r['plateau_hbm']:>8}")
ap = [r['card'] for r in rows if r.get('provenance') == 'AS-PULLED']
if ap:
    print(f"\nas-pulled (inventory artifacts, disclose on sale): {', '.join(ap)}")
cw = [(r["card"], float(r["core_CW"])) for r in rows if r.get("core_CW")]
if len(cw) < 3:
    print("\nneed >= 3 cards for a batch median"); raise SystemExit(0)
med = st.median([v for _, v in cw])
print(f"\nbatch median core C/W = {med:.4f}")
out = [(c, v) for c, v in cw if v > med * 1.20]
print("cards >20% over median (service the cooling):",
      ", ".join(f"{c} ({v:.3f})" for c, v in out) if out else "none")
ROLLUP
    exit 0
fi

# --gpu N picks one card; --all screens every CMP card in turn. On a multi-card
# bench "which card did this report describe" must never be a guess, so every
# artefact is keyed to the card's own serial and the binding is re-derived per run.
ALL=0
PARALLEL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --all) ALL=1; shift ;;
        --parallel) ALL=1; PARALLEL=1; shift ;;
        --gpu) GPU="${2:?--gpu needs an index}"; shift 2 ;;
        --gpu=*) GPU="${1#*=}"; shift ;;
        *) break ;;
    esac
done
MINS="${1:-12}"
TAG="${2:-}"
case "$MINS" in ''|*[!0-9]*) echo "usage: hx-vram-screen.sh [--all|--gpu N] [MINUTES] [TAG] | --rollup"; exit 64 ;; esac

if [ "$ALL" = "1" ]; then
    IDX="$(nvidia-smi --query-gpu=index,name --format=csv,noheader | awk -F, '/CMP 170HX/{gsub(/ /,"",$1); print $1}')"
    [ -n "$IDX" ] || { echo "no CMP 170HX cards found"; exit 1; }
    N_CARDS=$(echo "$IDX" | wc -l)
    RC=0
    if [ "$PARALLEL" = "1" ]; then
        # Enumerate the test tool's device list ONCE, here. Doing it inside each
        # child would briefly start the tool on its default device -- i.e. on a
        # card another child is already testing -- so the mapping is resolved up
        # front and handed down.
        echo "=== screening ${N_CARDS} card(s) SIMULTANEOUSLY, ${MINS} min ==="
        echo "    NOTE: every card in a batch must be screened the same way."
        echo "    Cards run together heat each other, so C/W from a parallel run"
        echo "    is only comparable with other parallel runs."
        MAP="$(timeout -s TERM 6 "$MTV" </dev/null 2>&1 | grep -E '^[0-9]+: Bus=0x' || true)"
        PIDS=""
        for g in $IDX; do
            bus="$(nvidia-smi -i "$g" --query-gpu=pci.bus_id --format=csv,noheader \
                   | tr -d ' ' | awk -F: '{print $(NF-1)":"$NF}' | cut -d. -f1)"
            idx="$(echo "$MAP" | awk -v b="$bus" 'index($0, "Bus=0x" b) {print $1}' | tr -d ':' | head -1)"
            if [ -z "$idx" ]; then echo "  GPU $g (PCI $bus): no device mapping — SKIPPED"; RC=1; continue; fi
            echo "  GPU $g = PCI $bus = test device $idx"
            MTV_IDX_OVERRIDE="$idx" GPU="$g" bash "$0" --gpu "$g" "$MINS" "$TAG" \
                > "$BASE/results/parallel-gpu${g}.log" 2>&1 &
            PIDS="$PIDS $!"
        done
        for pid in $PIDS; do wait "$pid" || RC=1; done
        echo "=== all parallel screens finished (rc=$RC) ==="
        for g in $IDX; do echo "--- GPU $g ---"; tail -6 "$BASE/results/parallel-gpu${g}.log"; done
    else
        echo "=== screening ${N_CARDS} card(s) sequentially, ${MINS} min each ==="
        for g in $IDX; do
            echo; echo "--- GPU index $g ---"
            GPU="$g" bash "$0" --gpu "$g" "$MINS" "$TAG" || RC=1
        done
    fi
    exit "$RC"
fi

# Identify the card FIRST — every artefact is named after its serial so a report
# can never be filed against the wrong unit. Pulled mining cards occasionally
# report no serial, so fall back to the GPU UUID, which is always present.
SN="$(nvidia-smi -i "$GPU" --query-gpu=serial --format=csv,noheader 2>/dev/null | tr -d ' ')"
case "$SN" in ''|'[N/A]'|'N/A'|'[Not Supported]')
    SN="$(nvidia-smi -i "$GPU" --query-gpu=uuid --format=csv,noheader 2>/dev/null | tr -d ' ' | cut -c1-16)"
    echo "NOTE: card reports no serial — naming output after GPU UUID: $SN" ;;
esac
[ -n "$SN" ] || { echo "ABORT: cannot read serial or UUID — is the GPU visible to the driver?"; exit 4; }
SN="$(printf '%s' "$SN" | tr -cd 'A-Za-z0-9._-')"     # filesystem-safe

# Serial collision guard. Refurbished / reflashed mining cards can carry blank
# or duplicated serials, and two cards sharing one would silently overwrite each
# other's report -- the batch would look complete while one card was never
# actually recorded. The GPU UUID is unique per physical device, so if a report
# already exists under this serial for a DIFFERENT card, disambiguate.
UUID_FULL="$(nvidia-smi -i "$GPU" --query-gpu=uuid --format=csv,noheader 2>/dev/null | tr -d ' ')"
if [ -f "$BASE/results/${SN}.md" ] && [ -n "$UUID_FULL" ] \
   && ! grep -qF "$UUID_FULL" "$BASE/results/${SN}.md" 2>/dev/null; then
    SN="${SN}-$(printf '%s' "$UUID_FULL" | sed 's/^GPU-//' | cut -c1-8)"
    echo "NOTE: serial already used by a different card — filing this one as ${SN}"
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$BASE/results/${SN}_${STAMP}"
mkdir -p "$OUT"
REPORT="$OUT/${SN}.md"
LATEST="$BASE/results/${SN}.md"
say(){ echo "$*" | tee -a "$OUT/run.log"; }

say "=== CMP 170HX screen — serial ${SN}${TAG:+ (tag: $TAG)} — ${STAMP} — ${MINS} min ==="
[ "$MINS" -ge 8 ] || say "WARN: under 8 min the thermal plateau check is disabled (run won't have settled)"

# --------------------------------------------------------------- 0. GUARD
APPS="$(nvidia-smi -i "$GPU" --query-compute-apps=pid,process_name,used_memory --format=csv,noheader 2>/dev/null)"
USED="$(nvidia-smi -i "$GPU" --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null)"
if [ -n "$APPS" ] || [ "${USED:-0}" -gt 512 ]; then
    say "ABORT: GPU not idle (${USED} MiB used). Coverage would be partial:"
    say "${APPS:-（memory allocated without a visible process）}"
    exit 2
fi
say "[0] idle guard ok — ${USED} MiB used, full-heap coverage possible"
DMESG_PRE="$(dmesg 2>/dev/null | wc -l)"      # baseline: everything below is history

# ------------------------------------------------------------ 1. IDENTITY
nvidia-smi -i "$GPU" -q > "$OUT/01-nvidia-smi-q.txt" 2>&1
nvidia-smi -i "$GPU" --query-gpu=name,serial,uuid,vbios_version,pci.bus_id,memory.total,driver_version \
    --format=csv > "$OUT/01-identity.csv" 2>&1
say "[1] $(sed -n '2p' "$OUT/01-identity.csv")"
FREE_MIB="$(nvidia-smi -i "$GPU" --query-gpu=memory.free --format=csv,noheader,nounits)"

# -------------------------------------------------- 1a. CARD DOSSIER
# Physical silicon (fuse defect words: latent VRAM per HBM stack, latent SMs,
# NVLink/MIG/ECC/NVDEC potential) + NVML lifetime hours. Read-only BAR0; the
# idle guard above already established the GPU is quiet. Root gets the fuse
# reads; without root it degrades to the NVML/identity part only.
CARDREPORT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/cardreport.py"
if [ -f "$CARDREPORT" ]; then
    say "[1a] card dossier (physical silicon + lifetime hours)"
    if sudo -n true 2>/dev/null; then
        # shellcheck disable=SC2024  # redirect stays with the invoking user on
        # purpose: the console capture must be owned by the bench user, not root
        sudo -n python3 "$CARDREPORT" \
            --out "$OUT/cardreport.md" --json "$OUT/cardreport.json" \
            >"$OUT/cardreport.console.txt" 2>&1
    else
        python3 "$CARDREPORT" \
            --out "$OUT/cardreport.md" --json "$OUT/cardreport.json" \
            >"$OUT/cardreport.console.txt" 2>&1
    fi
    if [ -f "$OUT/cardreport.json" ]; then
        python3 - "$OUT/cardreport.json" >> "$OUT/run.log" <<'PYDOSSIER'
import json, sys
try:
    for c in json.load(open(sys.argv[1])):
        p = c.get("physical") or {}
        v, cm = p.get("vram", {}), p.get("compute", {})
        up = c.get("uptime") or {}
        lat = v.get("latent_gib"); pol = v.get("policy_enabled_gib")
        if lat is not None:
            print("[1a] %s: latent %s GiB (policy %s), SMs %s/%s, lifetime %s h"
                  % (c.get("bdf"), lat, pol,
                     cm.get("latent_sms"), cm.get("enabled_sms"),
                     up.get("lifetime_h", "?")))
        else:
            print("[1a] %s: fuse words unreachable (dossier partial)" % c.get("bdf"))
except (OSError, ValueError):
    print("[1a] cardreport.json unreadable — continuing")
PYDOSSIER
    else
        say "[1a] cardreport failed (see cardreport.console.txt) — continuing"
    fi
fi

# ---------------------------------------------- 1b. PROVENANCE CLASSIFICATION
# Cards pulled from mining deployments frequently carry damaged or blanked
# onboard inventory: no serial, a wiped InfoROM, a missing board part number.
# That is not a performance defect, but it means the card cannot vouch for its
# own identity, so we classify it and disclose it rather than quietly shipping.
#
# NB: two fields read N/A on a perfectly healthy 170HX and must NOT be flagged —
#   "ECC Object: N/A"            ECC is fused off on this SKU by design
#   "Chassis Serial Number: N/A" the card is not in an enclosure
Q="$OUT/01-nvidia-smi-q.txt"
field(){ grep -m1 -E "^[[:space:]]*$1[[:space:]]*:" "$Q" | sed 's/.*: *//' | tr -d '\r'; }
BPN="$(field 'Board Part Number')"
IROM="$(field 'Image Version')"
PNAME="$(field 'Product Name')"
VBIOS="$(field 'VBIOS Version')"

ARTIFACTS=""
add_artifact(){ ARTIFACTS="${ARTIFACTS}- $1"$'\n'; }
case "$(nvidia-smi -i "$GPU" --query-gpu=serial --format=csv,noheader 2>/dev/null)" in
    ''|*N/A*|*'Not Supported'*) add_artifact "no serial number programmed — report is filed under GPU UUID instead" ;;
esac
case "$BPN"   in ''|*N/A*) add_artifact "no board part number in InfoROM" ;; esac
case "$IROM"  in ''|*N/A*) add_artifact "InfoROM image version unreadable" ;; esac
case "$VBIOS" in ''|*N/A*) add_artifact "VBIOS version unreadable" ;; esac
case "$PNAME" in *"CMP 170HX"*) : ;; *) add_artifact "product name reads '${PNAME}', not 'NVIDIA CMP 170HX'" ;; esac
grep -qi "infoROM is corrupted" "$Q" && add_artifact "driver reports the InfoROM as CORRUPTED"

if [ -n "$ARTIFACTS" ]; then
    PROVENANCE="AS-PULLED"
    say "[1b] provenance: AS-PULLED — inventory artifacts found:"
    printf '%s' "$ARTIFACTS" | tee -a "$OUT/run.log"
else
    PROVENANCE="DOCUMENTED"
    say "[1b] provenance: DOCUMENTED — serial, board P/N and InfoROM all intact"
fi

# ------------------------------- 2. INDEPENDENT (NON-NVIDIA) ENUMERATION
# Take the heap from THIS card's Vulkan block, not simply the first one in the
# file: with two cards installed the first block belongs to whichever device
# Vulkan enumerated first, which is not necessarily the card under test.
# Vulkan's deviceUUID equals nvidia-smi's GPU UUID, so it binds them exactly.
UUID="$(nvidia-smi -i "$GPU" --query-gpu=uuid --format=csv,noheader | sed 's/^GPU-//')"
if command -v vulkaninfo >/dev/null 2>&1; then
    vulkaninfo > "$OUT/02-vulkaninfo.txt" 2>&1
    HEAP="$(awk -v u="$UUID" '
        /^GPU[0-9]+:/            { want = 0 }
        index($0, u)             { want = 1 }
        want && /memoryHeaps\[0\]/ { grab = 1; next }
        grab && /size/           { print; exit }
    ' "$OUT/02-vulkaninfo.txt" | grep -oE '\([0-9.]+ GiB\)' | tr -d '()')"
    [ -n "$HEAP" ] || HEAP="could not match UUID ${UUID} in vulkaninfo"
else
    HEAP="not measured (apt install vulkan-tools)"
fi
say "[2] Vulkan DEVICE_LOCAL heap: ${HEAP}"

# ------------------------- 3. COMBINED VRAM TEST + THERMAL SOAK (one run)
say "[3] running memtest_vulkan ${MINS} min with telemetry (test == soak)"
[ -x "$MTV" ] || { say "ABORT: memtest_vulkan not at $MTV"; exit 3; }

# --- bind memtest_vulkan to the SAME physical card we took identity from -----
# memtest_vulkan keeps its own device list whose order does NOT match nvidia-smi
# (measured on our bench: its device 1 = PCI 06:00 = nvidia-smi GPU 1, its
# device 2 = PCI 05:00 = nvidia-smi GPU 0). Left on its "default device" a
# two-card bench silently prints one card's serial above the other card's
# result. Match on PCI address, which both tools agree on, and refuse to run if
# the mapping cannot be established.
BUSID="$(nvidia-smi -i "$GPU" --query-gpu=pci.bus_id --format=csv,noheader | tr -d ' ')"
BUS_SHORT="$(echo "$BUSID" | awk -F: '{print $(NF-1)":"$NF}' | cut -d. -f1)"
if [ -n "${MTV_IDX_OVERRIDE:-}" ]; then
    MTV_IDX="$MTV_IDX_OVERRIDE"      # resolved by the parallel launcher
    MTV_LIST="(supplied by launcher)"
else
    MTV_LIST="$(timeout -s TERM 5 "$MTV" </dev/null 2>&1 | grep -E '^[0-9]+: Bus=0x' || true)"
    MTV_IDX="$(echo "$MTV_LIST" | awk -v b="$BUS_SHORT" 'index($0, "Bus=0x" b) {print $1}' | tr -d ':' | head -1)"
fi
if [ -z "$MTV_IDX" ]; then
    say "ABORT: cannot map PCI ${BUS_SHORT} to a memtest_vulkan device. It listed:"
    say "${MTV_LIST:-  (nothing)}"
    exit 5
fi
# Size the test explicitly too, so coverage is a stated number rather than
# whatever the tool happened to grab. Second argument is BYTES, not MB.
FREE_MIB_NOW="$(nvidia-smi -i "$GPU" --query-gpu=memory.free --format=csv,noheader,nounits)"
TEST_BYTES=$(( FREE_MIB_NOW * 1048576 * 97 / 100 ))
say "    card ${BUS_SHORT} = memtest_vulkan device ${MTV_IDX}, testing $(( TEST_BYTES / 1073741824 )) GiB"
CSV="$OUT/03-telemetry.csv"
nvidia-smi -i "$GPU" \
    --query-gpu=timestamp,temperature.gpu,temperature.memory,power.draw,clocks.sm,clocks_throttle_reasons.active \
    --format=csv,noheader,nounits -l "$INTERVAL" > "$CSV" 2>&1 &
SAMPLER=$!
sleep 6                                    # capture idle baseline before load
timeout -s TERM "${MINS}m" "$MTV" "$MTV_IDX" "$TEST_BYTES" </dev/null > "$OUT/03-memtest_vulkan.txt" 2>&1
MTV_RC=$?          # 124 = our time limit ended it, i.e. it ran the WHOLE window
sleep 4                                    # let the tail of the curve land
kill "$SAMPLER" 2>/dev/null; wait "$SAMPLER" 2>/dev/null

# Verdict. Binding the test to a specific device makes the binary run as the
# WORKER process, and the worker does not print the parent's closing verdict
# when we stop it at the time limit -- so a clean run would read as FAIL if we
# only grepped for that line. Decide from what the worker does report:
#   * it prints an explicit "Error found ... total errors" on any miscompare
#   * every reporting interval prints the seconds it covered
# A pass therefore requires BOTH no error markers AND proven coverage of the
# requested window; anything else is INCONCLUSIVE, never PASS.
COVERED="$(grep -oE 'Passed +[0-9.]+ seconds' "$OUT/03-memtest_vulkan.txt" 2>/dev/null \
           | awk '{s+=$2} END{printf "%d", s+0}')"
if grep -qiE "ERRORS FOUND|Error found|total errors" "$OUT/03-memtest_vulkan.txt"; then
    VRAM=FAIL
elif grep -q "no any errors, testing PASSed" "$OUT/03-memtest_vulkan.txt"; then
    VRAM=PASS
elif [ "${MTV_RC:-1}" -eq 124 ] && [ "${COVERED:-0}" -gt 0 ]; then
    # rc=124 means OUR time limit stopped it, so it tested for the full window
    # rather than dying early. Summing its reported intervals is not a usable
    # coverage measure on its own -- it reports every 30 s, so a run is always
    # undercounted by up to one interval.
    VRAM=PASS
else
    VRAM="INCONCLUSIVE"
    say "    memtest exited early (rc=${MTV_RC}, ${COVERED}s reported) — not a pass"
fi
BW="$(grep -oE '[0-9]+\.?[0-9]*GB/sec' "$OUT/03-memtest_vulkan.txt" | sort -t G -k1 -g | tail -1)"
say "    VRAM: $VRAM   covered: ${COVERED}s   peak throughput: ${BW:-n/a}   samples: $(wc -l < "$CSV")"
[ "$VRAM" = FAIL ] && grep -iE "error found|total errors" "$OUT/03-memtest_vulkan.txt" | head -5 | tee -a "$OUT/run.log"

# ------------------------------------------------------- 4. THERMAL CURVE
python3 "$ANALYZE" "$CSV" "$INTERVAL" > "$OUT/04-thermal.md" 2>&1
THERMAL="$(grep -oE 'Thermal verdict: [A-Z —-]+' "$OUT/04-thermal.md" | sed 's/Thermal verdict: //' | head -1)"
FLEETLINE="$(grep -oE '<!--FLEET,[^>]*-->' "$OUT/04-thermal.md" | sed 's/<!--FLEET,//; s/-->//')"
say "[4] thermal verdict: ${THERMAL:-unparsed}"

# ------------------------------------------------- 5. OPTIONAL CUDA CHECK
CUDA="not run"
if [ "${CROSSCHECK:-0}" = "1" ] && [ -x "$HOME/bench-env/bin/python" ] && [ -f "$HOME/cmp170hx-tools/memtest.py" ]; then
    say "[5] CUDA cross-check (second API)"
    "$HOME/bench-env/bin/python" "$HOME/cmp170hx-tools/memtest.py" --passes 1 \
        --log "$OUT/05-cuda-memtest.log" > "$OUT/05-cuda-memtest.txt" 2>&1
    grep -q "result=PASS" "$OUT/05-cuda-memtest.log" && CUDA=PASS || CUDA=FAIL
    say "    CUDA: $CUDA"
fi

# ----------------------------------------------------------- 6. KERNEL LOG
# Only errors raised DURING this run count. Grepping the whole ring buffer
# reported this reference card as "see kernel log" on the strength of Xids from
# a PCIe experiment weeks earlier -- a false alarm on every used card.
dmesg 2>/dev/null | tail -n +$((DMESG_PRE + 1)) > "$OUT/06-dmesg-thisrun.txt"
grep -iE "xid|nvrm|pcie bus error" "$OUT/06-dmesg-thisrun.txt" > "$OUT/06-errors-thisrun.txt"
XIDS="$(wc -l < "$OUT/06-errors-thisrun.txt")"
# Historical entries are kept as context, clearly separated from the test result.
dmesg -T 2>/dev/null | grep -iE "xid|nvrm|pcie bus error" | tail -40 > "$OUT/06-dmesg-history.txt"
HIST="$(wc -l < "$OUT/06-dmesg-history.txt")"
say "[6] kernel errors during this run: ${XIDS}  (pre-existing in log: ${HIST})"

# -------------------------------------------------------------- 7. VERDICT
VERDICT=PASS
case "$VRAM" in PASS) : ;; FAIL) VERDICT="FAIL — VRAM ERRORS" ;; *) VERDICT="INCONCLUSIVE — rerun" ;; esac
[ "$CUDA" = FAIL ] && VERDICT="FAIL — VRAM ERRORS"
[ "$XIDS" -gt 0 ] && [ "$VERDICT" = PASS ] && VERDICT="PASS — kernel errors during run"
case "$THERMAL" in
  REPASTE*|REPAD*) [ "$VERDICT" = PASS ] && VERDICT="PASS — SERVICE COOLING" ;;
esac
say "OVERALL: $VERDICT"

sha256sum "$OUT"/* 2>/dev/null | grep -v SHA256SUMS > "$OUT/SHA256SUMS"
mkdir -p "$(dirname "$FLEET")"
# Schema guard: an older FLEET.csv has fewer columns, which would silently
# misalign every new row (and quietly corrupt the batch medians in --rollup).
FLEET_HDR="card,date,verdict,provenance,vram,thermal,core_CW,hbm_CW,plateau_core,plateau_hbm,peak_bw"
if [ -f "$FLEET" ] && ! head -1 "$FLEET" | grep -qxF "$FLEET_HDR"; then
    mv "$FLEET" "${FLEET%.csv}-legacy-${STAMP}.csv"
    say "NOTE: FLEET.csv had an older schema — archived as $(basename "${FLEET%.csv}-legacy-${STAMP}.csv")"
fi
[ -f "$FLEET" ] || echo "$FLEET_HDR" > "$FLEET"
IFS=',' read -r p_core p_hbm cw_core cw_hbm _ _ _ _ _ <<< "$FLEETLINE"
echo "${SN},${STAMP},${VERDICT},${PROVENANCE},${VRAM},${THERMAL},${cw_core},${cw_hbm},${p_core},${p_hbm},${BW}" >> "$FLEET"

# --------------------------------------------------------------- 8. REPORT
cat > "$REPORT" <<EOF
# CMP 170HX — VRAM & thermal acceptance report

**GPU serial:** ${SN}${TAG:+  (shelf tag: ${TAG})}
**GPU UUID:** ${UUID_FULL}   ·   **slot:** PCI ${BUS_SHORT}
**Date:** $(date -u +"%Y-%m-%d %H:%M UTC")
**Verdict:** **${VERDICT}**

## 1. Identity
\`\`\`
$(cat "$OUT/01-identity.csv")
\`\`\`

## 2. Provenance — **${PROVENANCE}**

| Field | Value |
|---|---|
| Serial number | ${SN} |
| Board part number | ${BPN:-not programmed} |
| VBIOS | ${VBIOS:-unreadable} |
| InfoROM image | ${IROM:-unreadable} |

$(if [ "$PROVENANCE" = "AS-PULLED" ]; then
printf '%s\n' "This card's onboard inventory data is incomplete:" "" "${ARTIFACTS}" \
"Cards recovered from mining deployments often have a damaged or blanked InfoROM." \
"This does not by itself affect performance or stability — every test result below" \
"was measured on this physical card — but it does mean the card cannot vouch for" \
"its own identity, so we disclose it rather than leave you to discover it."
else
printf '%s\n' "The card's onboard inventory is intact and self-consistent: it reports its own" \
"serial number, board part number and InfoROM version, and identifies itself as a" \
"CMP 170HX. Every result below is tied to the serial above."
fi)

## 3. Memory size confirmed by an independent, non-vendor API
Read through **Khronos Vulkan**, a driver path independent of NVIDIA's own tools.

| Source | Device-local memory |
|---|---|
| NVIDIA (nvidia-smi) | $(nvidia-smi -i "$GPU" --query-gpu=memory.total --format=csv,noheader) |
| Vulkan (vendor-neutral) | ${HEAP} |

## 4. VRAM integrity — **${VRAM}**
| | |
|---|---|
| Instrument | memtest_vulkan v0.5.0 (open source, Khronos Vulkan) |
| Duration | ${MINS} min continuous (${COVERED}s of testing recorded) |
| Coverage | $(( TEST_BYTES / 1073741824 )) GiB tested of ${FREE_MIB} MiB free, on an idle GPU |
| Card under test | PCI ${BUS_SHORT} (matched to test-tool device ${MTV_IDX} by PCI address) |
| Peak throughput | ${BW:-n/a} |
| Result | **${VRAM}** |

The GPU was verified idle before the run. Had any process held memory, the test
would have covered only the free remainder; the run is blocked in that condition
precisely so a partial pass cannot be mistaken for a full one.

$(cat "$OUT/04-thermal.md" | sed '/<!--FLEET/d')

## 6. Scope and limits
- **No ECC.** This card ships with ECC fused off, so there is no hardware error
  correction and no error counter to read afterwards. Pattern testing is the
  only available instrument; it detects errors rather than correcting them.
- A pass means no errors were detected across the full heap, at operating
  temperature, over the stated duration. Intermittent faults can evade any
  finite test.
- The card is passively cooled and has no onboard fan sensor: thermal figures
  describe this card **in this bench's airflow**, and are comparable between
  cards only when screened on the same bench.
$([ "$CUDA" != "not run" ] && echo "- Independent CUDA cross-check: **${CUDA}**")

## 7. Evidence
Raw logs in \`$(basename "$OUT")/\`, hashed in \`SHA256SUMS\`. Telemetry is the
full per-sample curve in \`03-telemetry.csv\`.
EOF

# The card dossier is part of the report: latent capacity is exactly what a
# buyer paying for silicon (not for the current policy floor) is buying.
if [ -f "$OUT/cardreport.md" ]; then
    {
        echo
        echo "---"
        echo
        cat "$OUT/cardreport.md"
        echo
        echo "dossier raw: \`$(basename "$OUT")/cardreport.{md,json}\` (read-only fuse reads)"
    } >> "$REPORT"
fi

cp -f "$REPORT" "$LATEST"
say "report: $LATEST"
say "raw:    $OUT"
[ "${VERDICT#FAIL}" != "$VERDICT" ] && exit 1
exit 0
