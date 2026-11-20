# Test your GPU

**Bought a used NVIDIA CMP 170HX? Don't take anyone's word for it — including
ours. Run the test yourself.**

This is the acceptance screen we run on every card before we sell it, published
in full: full-heap VRAM integrity, thermal triage, and provenance disclosure,
ending in a per-card report with hashed raw evidence behind every number.

One command, about fifteen minutes, and you have the same document we have — for
your card, on your bench, from your own run. If our report and yours disagree,
that is something you can now find out.

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

See [`examples/`](examples/) for a full PASS report, a real FAIL, and the batch
rollup they came from.

---

## What it actually proves

Four claims about your card, each with its own instrument, and none of them
taken on trust from a single vendor tool:

| Claim | How | Why it is not circular |
|---|---|---|
| The card is **this** card | Report is named after the GPU serial read off the card, with the UUID alongside | A report can never be filed against the wrong unit; duplicate serials are disambiguated by UUID |
| It really has **64 GiB** | `vulkaninfo` DEVICE_LOCAL heap, matched to this card by `deviceUUID` | Khronos Vulkan is a **non-NVIDIA** path — independent corroboration of `nvidia-smi` |
| The HBM runs at **full speed** | STREAM-style kernels vs a peak derived from the card's own bus width and memory clock | Nothing is looked up from a spec sheet; both operands are read off the device |
| The memory is **sound** | `memtest_vulkan` pattern test over the whole free heap, on a verified-idle GPU | Coverage is a stated number of GiB, not "whatever the tool grabbed" |
| The cooling is **intact** | Per-sample telemetry through the run → °C/W for core and HBM, compared against the batch median | Identical workload on every card ⇒ near-identical power ⇒ temperature is a direct read-out of heat transfer |
| The silicon is **worth its price** | Card dossier: fuse defect words → latent VRAM per HBM stack and latent SM count; NVML counter → lifetime power-on hours | Read from the card's own fuse registers — not the policy floor the driver shows, and not anything the seller configured |

And one it discloses rather than proves: **provenance**. Ex-mining cards often
have a blanked or damaged InfoROM. That is not a performance defect, but it
means the card cannot vouch for its own identity, so the report says so under an
`AS-PULLED` heading instead of leaving you to find out later.

The full reasoning, including what a pass does *not* mean, is in
**[docs/METHODOLOGY.md](docs/METHODOLOGY.md)**.

## The card dossier

Every report ends with a dossier read straight off the card's fuse registers
(read-only BAR0 — no commands issued, no driver state touched):

- **Physical VRAM** — the FBP defect words say how much memory the die actually
  has, per HBM stack, up to the 96 GiB full bin. A card showing 64 GiB to the
  driver can be an 80 GiB die behind a policy floor — that is silicon you are
  paying for (or not).
- **Compute** — latent vs enabled SMs from the GPC/TPC defect and disable
  words, per GPC.
- **Lifetime hours** — the driver's cumulative BBX counter, i.e. real power-on
  history, not a sticker claim.
- **Features** — NVLink, MIG/ECC/NVDEC and PCIe generation disable words:
  what the die could do versus what the policy allows.
- **Burn history** — with `--ffrows` (idle card), the physical fuse-row walk
  shows whether the rewritable FF rows carry records and how much of the
  append-only correction budget remains.

It also runs standalone:

```console
$ sudo python3 bin/cardreport.py            # full dossier, every GA100 card
$ python3 bin/cardreport.py                 # identity + lifetime hours only
$ python3 bin/cardreport.py --selftest      # offline analytics validation
```

Register addresses from public GA100 research (kate-land GA100VFI capture
data, live-confirmed on CMP 170HX by the honzapatCZ cmp170hx-test project).
Non-GA100 NVIDIA cards in the box are listed but skipped — the fuse map is
GA100-only, and applying it anywhere else would print fiction.

## Quick start

Requires a 170HX that is already unlocked and visible to the driver — that is a
one-time bench job, see **[bench-setup/](bench-setup/)**.

```console
$ git clone https://github.com/apoage/cmp-170hx-screening
$ cd cmp-170hx-screening
$ bash install.sh --service          # deps + memtest_vulkan + hx command + dashboard on :8097

$ hx 15                              # screen the card in slot 0, 15 minutes
$ hx --card                          # card dossier alone (no soak)
$ hx --all 15                        # every 170HX in turn
$ hx --parallel 15                   # every 170HX simultaneously
$ hx --rollup                        # compare the batch
```

(`install.sh` puts the `hx` command in `/usr/local/bin` when it has sudo, else
`~/.local/bin`. The long forms — `bash bin/hx-vram-screen.sh 15`,
`sudo python3 bin/cardreport.py` — work straight from the checkout too.)

Results land in `~/vramscreen/results/` (override with `HX_BASE`):

```
<SERIAL>.md                 the report to send the buyer
<SERIAL>_<timestamp>/       raw logs, telemetry CSV, SHA256SUMS
FLEET.csv                   one row per run — the batch record
```

**Run at least 8 minutes**, and 15 for a report you will send to someone. The
card is still climbing at 84 s; a short run cannot distinguish "hot card" from
"not settled yet", and the analyser disables the plateau check below 5 min
rather than print a number it cannot stand behind.

### Memory throughput

Soundness and speed are different claims. A card can pass the memory test and
still be slow, so throughput is measured separately:

```console
$ ~/vllm-env/bin/python bin/hx-bandwidth.py       # needs torch + CUDA
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

**An unlocked 170HX beats its own published memory-bandwidth figure**, and the
arithmetic says exactly why:

| | bus | memory clock | bandwidth |
|---|---|---|---|
| Published CMP 170HX spec | 4096-bit | 1.458 GHz | 1493 GB/s |
| What the card reports unlocked | 4096-bit | **1.728 GHz** | 1769 GB/s theoretical |
| Measured here (STREAM read) | | | **1650 GB/s** |

Same bus width; the unlock lifts the memory clock **+18.5 %**. 1493 GB/s is not
a hardware limit, it is the restricted state — and `4096 × 1.458 GHz × 2 ÷ 8`
reproduces the published figure to the GB, which is how you know that reading is
right rather than convenient.

## What is in here

| | |
|---|---|
| `bin/hx-vram-screen.sh` | The screen. One continuous run: memory test *is* the thermal soak |
| `bin/cardreport.py` | Card dossier: physical VRAM/SM potential from fuse defect words, lifetime power-on hours, feature masks. Stdlib only, read-only |
| `bin/hx-thermal-analyze.py` | Turns the telemetry curve into °C/W and a repaste/repad verdict |
| `bin/hx-bandwidth.py` | HBM2e throughput, STREAM-style, as a measured fraction of derived peak |
| `bin/hx-dashboard.py` | Web UI on `:8097` — batch index, live run, customer-ready export. Stdlib only, read-only |
| `bin/hx-tui.py` | Console display for a headless bench, on `tty1` or over `ssh -t` |
| `bench-setup/` | Driver, unlock, airflow, fan wiring — the one-time bench build |
| `docs/METHODOLOGY.md` | Why each metric, and the limits of the result |
| `examples/` | A real PASS, a real FAIL, and the batch rollup |

The dashboard is deliberately **stdlib-only and read-only**: it runs unattended
from boot on a bench box with no pip and no network, and it cannot disturb a
screen in progress.

## Verdicts

| Verdict | Meaning |
|---|---|
| `PASS` | No memory errors, no kernel errors during the run, thermals in band |
| `PASS — SERVICE COOLING` | Memory is sound; the card is a repaste/repad candidate |
| `PASS — kernel errors during run` | Memory test clean but the kernel logged Xid/NVRM. Investigate before shipping |
| `FAIL — VRAM ERRORS` | Miscompares found. Do not ship |
| `INCONCLUSIVE — rerun` | The test did not run the full window. **Never** read as a pass |

There is no verdict for "probably fine". A run that cannot prove it covered its
window is `INCONCLUSIVE`, by design — see
[why that distinction is load-bearing](docs/METHODOLOGY.md#why-inconclusive-is-not-a-pass).

## Scope

This is written for the **CMP 170HX** specifically: its 250 W software power
cap, its HBM2e temperature sensor, its fused-off ECC, its factory-floorswept
FBPAs. The structure — idle guard, independent enumeration, test-as-soak,
batch-median triage — generalises to any passively cooled accelerator, but the
thresholds do not. Do not point it at a different card and trust the numbers.

## Third-party components

The pattern-test engine underneath is
**[GpuZelenograd/memtest_vulkan](https://github.com/GpuZelenograd/memtest_vulkan)**
(note: *not* GpuZoo, a different project). `install.sh` fetches it from its own
release page rather than vendoring it, so you can verify the binary you run.
Everything around it — the idle guard, the coverage accounting, the device
binding, the thermal analysis, the verdict logic and the reports — is this
repository.

Unlocking the card is a prerequisite, not part of this suite, and is somebody
else's work: [cmpunlocker](https://github.com/amoghmunikote/cmpunlocker)
(GPL-2.0). None of its code is here; `bench-setup/` only sequences it.

## Licence

MIT — see [LICENSE](LICENSE). The reports it generates are yours.
