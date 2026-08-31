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
| The memory is **sound** | `memtest_vulkan` pattern test over the whole free heap, on a verified-idle GPU | Coverage is a stated number of GiB, not "whatever the tool grabbed" |
| The cooling is **intact** | Per-sample telemetry through the run → °C/W for core and HBM, compared against the batch median | Identical workload on every card ⇒ near-identical power ⇒ temperature is a direct read-out of heat transfer |

And one it discloses rather than proves: **provenance**. Ex-mining cards often
have a blanked or damaged InfoROM. That is not a performance defect, but it
means the card cannot vouch for its own identity, so the report says so under an
`AS-PULLED` heading instead of leaving you to find out later.

The full reasoning, including what a pass does *not* mean, is in
**[docs/METHODOLOGY.md](docs/METHODOLOGY.md)**.

## Quick start

Requires a 170HX that is already unlocked and visible to the driver — that is a
one-time bench job, see **[bench-setup/](bench-setup/)**.

```console
$ git clone https://github.com/YOUR-ORG/cmp-170hx-screening
$ cd cmp-170hx-screening
$ bash install.sh --service          # deps + memtest_vulkan + dashboard on :8097

$ bash bin/hx-vram-screen.sh 15      # screen the card in slot 0, 15 minutes
$ bash bin/hx-vram-screen.sh --all 15        # every 170HX in turn
$ bash bin/hx-vram-screen.sh --parallel 15   # every 170HX simultaneously
$ bash bin/hx-vram-screen.sh --rollup        # compare the batch
```

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

## What is in here

| | |
|---|---|
| `bin/hx-vram-screen.sh` | The screen. One continuous run: memory test *is* the thermal soak |
| `bin/hx-thermal-analyze.py` | Turns the telemetry curve into °C/W and a repaste/repad verdict |
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
