# cmp-170hx-screening

Bench tooling for the NVIDIA CMP 170HX (GA100): an acceptance screen that
fuses a full-heap VRAM pattern test with a thermal soak in one run, a card
dossier read from the fuse registers over BAR0, and an identification mode —
all stdlib-only Python plus one external test binary, ending in per-card
reports with hashed raw evidence behind every number.

```console
$ bash bin/hx-vram-screen.sh 15
=== CMP 170HX screen — serial 1322621098890 — 20260828-094851 — 15 min ===
[0] idle guard ok — 1 MiB used, full-heap coverage possible
[1] NVIDIA CMP 170HX, 1322621098890, GPU-b638ea07-…, 92.00.6D.00.0A, 65536 MiB
[1a] 0000:05:00.0: latent 80 GiB (policy 64), SMs 108/70, lifetime 4164.9 h
[1b] provenance: DOCUMENTED — serial, board P/N and InfoROM all intact
[2] Vulkan DEVICE_LOCAL heap: 64.00 GiB
[3] running memtest_vulkan 15 min with telemetry (test == soak)
    card 05:00 = memtest_vulkan device 2, testing 61 GiB
    VRAM: PASS   covered: 848s   peak throughput: 937.6GB/sec   samples: 452
[4] thermal verdict: OK
[6] kernel errors during this run: 0  (pre-existing in log: 0)
OVERALL: PASS
report: ~/vramscreen/results/1322621098890.md
```

Real unedited runs (a PASS, a FAIL, the batch rollup) are in
[`examples/`](examples/).

## What it measures

Six measurements, each with its own instrument, none taken on trust from a
single vendor tool:

| Measurement | Instrument | Why it is not circular |
|---|---|---|
| Identity | GPU serial read off the card names every artefact; UUID alongside | A report cannot be filed against the wrong unit; duplicate serials are disambiguated by UUID |
| Heap size | `vulkaninfo` DEVICE_LOCAL heap, matched to this card by `deviceUUID` | Khronos Vulkan is a **non-NVIDIA** path — independent corroboration of `nvidia-smi` |
| HBM throughput | STREAM-style kernels vs a peak derived from the card's own bus width and memory clock | Nothing is looked up from a spec sheet; both operands are read off the device |
| Memory integrity | `memtest_vulkan` pattern test over the whole free heap, on a verified-idle GPU | Coverage is a stated number of GiB, not "whatever the tool grabbed" |
| Thermal path | Per-sample telemetry through the run → °C/W for core and HBM, compared against the batch median | Identical workload on every card ⇒ near-identical power ⇒ temperature is a direct read-out of heat transfer |
| Latent silicon | Card dossier: fuse defect words → latent VRAM per HBM stack and latent SM count; NVML counter → driver-recorded hours | Read from the card's own fuse registers — not the policy floor the driver shows. Latent = what the die has behind policy, not a promise of extra usable capacity |

One property is disclosed rather than measured: **provenance**. Ex-mining
cards often have a blanked or damaged InfoROM; that is not a performance
defect, but it means the card cannot vouch for its own identity, so the
report records it under an `AS-PULLED` heading.

The full reasoning, including what a pass does *not* mean, is in
**[docs/METHODOLOGY.md](docs/METHODOLOGY.md)**.

## Card dossier

Every report ends with a dossier read straight off the card's fuse registers
(read-only BAR0 — no commands issued, no driver state touched):

- **Physical VRAM** — the FBP defect words state how much memory the die has,
  per HBM stack, up to the 96 GiB full bin. A card exposing 64 GiB to the
  driver can be an 80 GiB die behind a policy floor.
- **Compute** — latent vs enabled SMs from the GPC/TPC defect and disable
  words, per GPC.
- **Uptime telemetry** — the driver's cumulative BBX counter: hours recorded
  since the InfoROM was last initialised. A wiped InfoROM restarts it, so low
  hours on a wiped card mean unknown, not new.
- **Features** — NVLink, MIG/ECC/NVDEC and PCIe generation disable words:
  what the die could do versus what the policy allows.
- **Burn history** — with `--ffrows` (idle card), the physical fuse-row walk
  shows whether the rewritable FF rows carry records and how much of the
  append-only correction budget remains.

Standalone:

```console
$ sudo python3 bin/cardreport.py            # full dossier, every GA100 card
$ python3 bin/cardreport.py                 # identity + uptime only (no root)
$ sudo python3 bin/cardreport.py --ffrows   # physical fuse-row walk (idle card)
$ python3 bin/cardreport.py --selftest      # offline analytics validation
```

Register addresses from public GA100 research (kate-land GA100VFI capture
data, live-confirmed on CMP 170HX by the honzapatCZ cmp170hx-test project).
Non-GA100 NVIDIA cards in the box are listed but skipped — the fuse map is
GA100-only, and applying it anywhere else would print fiction.

## Requirements

- Linux, Python 3 (stdlib only), `nvidia-smi` from the loaded driver
- Root (or passwordless `sudo -n`) for the BAR0 fuse reads and the kernel log;
  everything else runs unprivileged
- `vulkan-tools` (independent heap enumeration), `memtest_vulkan`
  (fetched by `install.sh` from its own releases)
- **An unlocked card.** The screen aborts on a locked one (< 16 GiB total):
  the heap under test must be the card's operating configuration or the
  results describe hardware nobody runs. Install
  [CMPUnlocker](https://github.com/amoghmunikote/cmpunlocker) via
  [bench-setup/hx-install-unlock.sh](bench-setup/), cold boot, then screen.

## Usage

```console
$ git clone https://github.com/apoage/cmp-170hx-screening
$ cd cmp-170hx-screening
$ bash install.sh [--service]      # deps + memtest_vulkan + hx command
                                   # --service also adds the dashboard unit

$ hx 15                            # screen card in slot 0, 15 minutes
$ hx --identify                    # report of what the card IS — no test run
$ hx --card                        # card dossier alone (console only)
$ hx --all 15                      # every 170HX in turn
$ hx --parallel 15                 # every 170HX simultaneously
$ hx --rollup                      # compare the batch
```

`install.sh` puts `hx` in `/usr/local/bin` when it has sudo, else
`~/.local/bin`. Long forms (`bash bin/hx-vram-screen.sh 15`,
`sudo python3 bin/cardreport.py`) work straight from the checkout.

| Env | Default | Meaning |
|---|---|---|
| `HX_BASE` | `~/vramscreen` | results root |
| `HX_PORT` / `HX_BIND` | `8097` / `127.0.0.1` | dashboard port / bind address |
| `GPU` | first 170HX | `nvidia-smi` index to screen |
| `MTV` / `MTV_VERSION` | fetched | memtest_vulkan path / version |
| `CROSSCHECK` + `CROSSCHECK_PY` | off | optional second-API CUDA memory check |

**Run at least 8 minutes** (15 recommended): the card is still climbing at
84 s, and a short run cannot distinguish "hot card" from "not settled yet".
The analyser disables the plateau check below 5 min rather than print a
number it cannot stand behind.

### Outputs

```
$HX_BASE/results/
  <SERIAL>.md                 per-card report (latest run of this serial)
  <SERIAL>-identity.md        identification-only report (--identify)
  <SERIAL>_<timestamp>/       raw logs, telemetry CSV, SHA256SUMS
  FLEET.csv                   one row per run — the batch record
```

Exit codes are a contract for wrappers and `--all`: **0** PASS-family,
**1** FAIL, **2** INCONCLUSIVE.

### Memory throughput

Integrity and speed are different measurements; throughput is separate:

```console
$ <your-torch-venv>/bin/python bin/hx-bandwidth.py   # needs torch + CUDA
card        : NVIDIA CMP 170HX  (70 SMs, 68.2 GB, 34 MB L2)
memory      : 4096-bit HBM2e @ 1.728 GHz
derived peak: 1769 GB/s  (= 4096 bit x 1.728 GHz x 2 DDR)

  read   (sum)      1650.5 GB/s best    1644.9 median    93.3% of peak
  write  (fill)     1436.8 GB/s best    1435.4 median    81.2% of peak
  copy   (a=b)      1589.2 GB/s best    1587.4 median    89.8% of peak
  triad  (a=b+sc)   1595.6 GB/s best    1592.6 median    90.2% of peak

headline    : 1650 GB/s (93% of derived peak 1769 GB/s)
mem clock   : 1728 / 1728 MHz under load
```

An unlocked 170HX exceeds its published memory-bandwidth figure, and the
arithmetic says exactly why:

| | bus | memory clock | bandwidth |
|---|---|---|---|
| Published CMP 170HX spec | 4096-bit | 1.458 GHz | 1493 GB/s |
| What the card reports unlocked | 4096-bit | **1.728 GHz** | 1769 GB/s theoretical |
| Measured here (STREAM read) | | | **1650 GB/s** |

Same bus width; the unlock lifts the memory clock **+18.5 %**. 1493 GB/s is
not a hardware limit, it is the restricted state — and
`4096 × 1.458 GHz × 2 ÷ 8` reproduces the published figure to the GB, which
is how you know that reading is right rather than convenient.

## Verdicts

Memory errors are graded, not just counted:

| VRAM result | Meaning |
|---|---|
| `PASS` | No errors detected over proven coverage of the tested window (~97 % of the free heap) |
| `PASS-ECC` | Pattern-clean, but the ECC counters advanced with **corrected** (single-bit) events during the run — disclosed as a finding |
| `MASK-CANDIDATE` | Errors exist whose buffer addresses sit high enough that a smaller unlock *might* exclude them. **Not a pass** — buffer position is not physical position (HBM is interleaved). The card fails until a retest at the smaller geometry actually passes |
| `FAIL` | Errors reach below the smallest standard geometry (40 GiB), or ECC logged **uncorrectable** events |

| Overall verdict | Meaning |
|---|---|
| `PASS` | No memory errors, no kernel errors during the run, thermals in band |
| `PASS — ECC-corrected events disclosed` | `PASS-ECC` above |
| `FAIL — MASK CANDIDATE (unverified)` | `MASK-CANDIDATE` above — a passing retest at the smaller geometry is the only path to a pass |
| `PASS — SERVICE COOLING` | Memory is sound; the cooling path needs servicing (repaste/repad) |
| `PASS — kernel errors during run` | Memory test clean but the kernel logged Xid/NVRM — investigate |
| `FAIL — VRAM ERRORS` / `FAIL — FATAL GPU ERROR (Xid …)` | Not usable |
| `INCONCLUSIVE — rerun` | The test did not prove its coverage window. **Never** read as a pass |

There is no verdict for "probably fine". A run that cannot prove it covered
its window is `INCONCLUSIVE`, by design — see
[why that distinction is load-bearing](docs/METHODOLOGY.md#why-inconclusive-is-not-a-pass).

## Repository layout

| | |
|---|---|
| `bin/hx-vram-screen.sh` | The screen. One continuous run: memory test *is* the thermal soak |
| `bin/cardreport.py` | Card dossier: physical VRAM/SM potential from fuse defect words, uptime telemetry, feature masks. Stdlib only, read-only |
| `bin/hx-vram-verdict.py` | Verdict classifier: coverage accounting, error-address analysis, ECC deltas. Fixture-tested in CI |
| `bin/hx-thermal-analyze.py` | Turns the telemetry curve into °C/W and a repaste/repad verdict |
| `bin/hx-bandwidth.py` | HBM2e throughput, STREAM-style, as a measured fraction of derived peak |
| `bin/hx-dashboard.py` | Web UI on `:8097` — batch index, live run, export. Stdlib only, read-only |
| `bin/hx-tui.py` | Console display for a headless bench, on `tty1` or over `ssh -t` |
| `bench-setup/` | Driver, unlock sequencing, airflow, fan wiring — the one-time bench build |
| `docs/METHODOLOGY.md` | Why each metric, and the limits of the result |
| `examples/` | A real PASS, a real FAIL, and the batch rollup |
| `tests/fixtures/` | Captured and synthetic logs the CI runs the classifiers against |

The dashboard is deliberately **stdlib-only and read-only**: it runs unattended
from boot on a bench box with no pip and no network, and it cannot disturb a
screen in progress. It has **no login** and serves `nvidia-smi -q` and
kernel-log excerpts, so the binary itself defaults to `127.0.0.1`;
`install.sh --service` deliberately opts into the bench LAN with
`HX_BIND=0.0.0.0` (drop that line in `bench-setup/hx-dashboard.service.in` to
keep it loopback).

## Scope

Written for the **CMP 170HX** specifically: its 250 W software power cap, its
HBM2e temperature sensor, its fused-off ECC, its factory-floorswept FBPAs.
The structure — idle guard, independent enumeration, test-as-soak,
batch-median triage — generalises to any passively cooled accelerator, but
the thresholds do not. Do not point it at a different card and trust the
numbers.

## Third-party components

The pattern-test engine underneath is
**[GpuZelenograd/memtest_vulkan](https://github.com/GpuZelenograd/memtest_vulkan)**
(note: *not* GpuZoo, a different project). `install.sh` fetches it from its
own release page rather than vendoring it, so you can verify the binary you
run. Everything around it — the idle guard, the coverage accounting, the
device binding, the thermal analysis, the verdict logic and the reports — is
this repository.

Unlocking the card is a prerequisite, not part of this suite, and is somebody
else's work: [cmpunlocker](https://github.com/amoghmunikote/cmpunlocker)
(GPL-2.0). None of its code is here; `bench-setup/` only sequences it.

## Licence

MIT — see [LICENSE](LICENSE).
