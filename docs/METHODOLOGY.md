# Methodology

How we decide whether a used CMP 170HX is fit to sell, and what our verdict does
and does not mean.

Everything below was measured on real cards, not inferred from datasheets. Where
a design choice exists only because a naive version of the test gave us a wrong
answer, that is stated — those are the parts worth copying.

---

## 1. The card, and why it needs a bespoke test

The CMP 170HX is a GA100 mining card. Three of its properties shape the whole
method:

**No ECC.** ECC is fused off on this SKU. There is no hardware correction and no
error counter to read afterwards. On a datacentre card you can run a workload
and then ask the card how many errors it corrected; here that instrument does
not exist. **Pattern testing is the only available instrument**, and it detects
errors rather than correcting them. Every report says so.

**Passively cooled, no fan sensor.** `fan.speed` reads N/A. The card cannot
regulate its own temperature and cannot tell you how much air it is getting, so
an absolute temperature is a statement about the *bench*, not the card. This is
why the method is comparative (§5).

**FBPAs floorswept at the factory.** Our reference card reports
`FBPA_DISABLE=0xc0330c` — eight channel disables across the FBPA groups — and
still measures 1650 GB/s, 93 % of the peak implied by its own 4096-bit bus
(§5b). So a healthy card of this SKU does *not* have every channel enabled,
and **"all memory channels available" is a claim you must not make**. What we
claim instead: the heap the driver exposes is present and addressable, and
throughput is in the healthy band. Both are measured.

The card dossier (§1a) measures capacity at the coarser **FBP partition**
level — the defect words say how many of the 12 partitions (and therefore how
many of the 6 HBM stacks, and how many GiB) the die physically has behind its
policy floor. Latent capacity is *potential silicon*, not a promise of usable
memory: channel-level (FBPA) floorsweeping inside a live partition is exactly
why the dossier never claims "all channels", only partition-level capacity.
The two granularity levels are consistent: partitions say how big the die is;
FBPA words and measured throughput say how completely each partition is
wired.

## 2. Identity before anything else

The screen reads the GPU serial off the card and names every artefact after it —
the report, the raw-log directory, the row in `FLEET.csv`. A report therefore
cannot be attached to the wrong unit by a filing mistake.

Two failure modes on ex-mining stock forced extra handling:

- **No serial programmed.** Falls back to the GPU UUID, and says so in the
  report.
- **Duplicate serials.** Refurbished and reflashed cards do turn up sharing a
  serial. Two of them would silently overwrite each other's report — the batch
  would look complete while one card was never actually recorded. The UUID is
  unique per physical device, so a colliding card is filed as
  `<serial>-<uuid8>`.

`Board Part Number` is **not** a serial. `900-11001-0108-000` is the SKU and is
identical on every 170HX. If you see it varying, something else is wrong.

## 3. The idle guard

> `memtest_vulkan` tests only the VRAM that is **free when it starts.**

With an inference server resident, we watched it print
`no any errors, testing PASSed` having covered **7 GB of 64 GB** — output
character-for-character indistinguishable from a real full-heap pass.

So the screen **refuses to start** unless the GPU is idle (no compute processes,
under 512 MiB allocated), and the report states the coverage in GiB against the
free memory it measured. A partial pass that reads like a full one is worse than
no test at all, because it is a false assurance you would then put in writing.

The test size is also passed explicitly rather than left to the tool's default,
so coverage is a number we chose and can quote, not a number we inherited.

## 4. Independent corroboration of the 64 GiB

"The vendor's tool says the card has 64 GiB" is a weak claim to a buyer who is
worried about a reflashed card. So the screen also reads the DEVICE_LOCAL heap
through **Khronos Vulkan**, a driver path independent of NVIDIA's own tooling,
and puts both figures side by side.

Vulkan works on this card — it enumerates as a discrete GPU (API 1.4.341) and
NVIDIA's ICD ships with the patched driver. Only `vulkan-tools` is missing from
a stock install.

The heap is matched to the card under test by **`deviceUUID`**, which equals the
`nvidia-smi` GPU UUID. Taking `memoryHeaps[0]` from the first block in the file
is wrong on any multi-card bench: the first block belongs to whichever device
Vulkan happened to enumerate first, which need not be the card being tested.

## 5. The test is the soak

The memory test drives the card from 39 W to full load in about **two seconds**
and holds it there for the entire run. There is no reason to run a separate
thermal stress test — running one would only mean the thermal data and the
memory data describe two different thermal states.

So it is **one continuous run**, with `nvidia-smi` telemetry sampled every 2 s
throughout: temperature (core and HBM), power, SM clock, throttle mask.

**Why this yields a comparable metric.** Every card runs the identical workload
and therefore dissipates near-identical power. The temperature it reaches is
then a direct read-out of how well heat leaves the die — the paste, the pads,
the heatsink mount. Dividing the rise over idle by the **measured average power**
gives °C/W, which *is* comparable card-to-card. Absolute temperatures are not.

Power is not a fixed number, and the analyser does not assume one: a small
working set sits at the ~250 W software cap (throttle mask `0x4`, expected), the
full 63 GiB heap sustains nearer 200–220 W. Because the divisor is measured per
run, °C/W stays comparable either way — but for the same reason you should quote
the wattage from the report rather than a nominal figure.

### The triage signals, in order of confidence

1. **Thermal throttle asserted** (mask `0x20`/`0x40`) — urgent, the card is
   losing performance right now.
2. **Still climbing at end of run** — heat is not getting out.
3. **High plateau at fixed power** — poor interface.
4. **Wide memory-over-core gap under load** — the HBM pads, not the die paste.

Signal 4 is the one that pays for the HBM sensor. `temperature.memory` works on
this card and idles ~16 °C *above* core. A card whose memory runs unusually hot
*relative to its own core* needs the pads over the HBM stacks replaced — a
different repair from a repaste, and one you would misdiagnose from core
temperature alone.

Worked example from our batch, same bench, same 15-minute parallel run:

| serial | core °C/W | HBM °C/W | plateau core / HBM | idle gap | loaded gap |
|---|---|---|---|---|---|
| …0123 | 0.093 | 0.083 | 67 / 69 °C | 4 °C | 2 °C |
| …9588 | **0.137** | **0.110** | 66 / **75** °C | **15 °C** | **9 °C** |

Card …9588 moves heat about 47 % worse. Its core plateau is unremarkable — it is
the *gap* that indicts the pads. Service candidate, not a reject.

### Run length

Reference ramp from idle: core 30 → 58 °C and HBM 46 → 68 °C in **84 seconds,
still climbing**. A run under ~8 minutes therefore cannot distinguish a hot card
from a card that has not settled. The analyser **disables the plateau check
below 5 minutes** rather than emit a verdict it cannot support; the screen warns
below 8. Use 15 for anything you will send to a buyer.

## 5b. Throughput: measured against a peak the card derives itself

The memory test says the heap is *sound*. It does not say it is *fast*, and the
GB/sec it prints is not a bandwidth measurement — `memtest_vulkan` generates and
verifies patterns while it streams, so ALU work and read-back are folded into
that figure. On our cards it reads ~940 GB/s, which is a **floor**.

`hx-bandwidth.py` runs STREAM-style kernels that do nothing but move bytes —
read, write, copy, scale, add, triad — each paired with the number of bytes it
is *required* to move. Getting that accounting wrong is the classic way to
publish a bandwidth figure that is a clean factor of 2 or 3 out.

### The peak comes off the card, not off a spec sheet

Theoretical peak is derived from two values read from the device itself:

```
bus width x memory clock x 2 (DDR) / 8
4096 bit x 1.728 GHz x 2 / 8      = 1769 GB/s
```

This matters more than it looks, because **the published CMP 170HX figure is
1493 GB/s** — lower than what the card reports. The same arithmetic explains the
gap exactly:

| | bus | memory clock | bandwidth |
|---|---|---|---|
| Published spec | 4096-bit | 1.458 GHz | **1493 GB/s** |
| Unlocked, as reported by the device | 4096-bit | **1.728 GHz** | 1769 GB/s |

`4096 × 1.458 GHz × 2 ÷ 8` reproduces the published number **to the GB**. So the
bus width is stock, and the restriction was on the memory clock: unlocking lifts
it **+18.5 %**. 1493 GB/s is a state, not a ceiling.

That reproduction is the reason to trust the derived peak at all. A "theoretical
peak" you cannot check against a known-good reference is just a bigger number to
divide by.

### Measured, on a card with a tenant resident

| kernel | bytes moved | best | % of derived peak |
|---|---|---|---|
| read (sum) | N | **1650 GB/s** | 93 % |
| write (fill) | N | 1437 GB/s | 81 % |
| copy | 2N | 1589 GB/s | 90 % |
| add / triad | 3N | 1596 GB/s | 90 % |

Serial 1322421002084, memory clock 1728/1728 MHz under load, 3 × 1420 MiB
working set. Note the conditions: this card had an inference server holding
58.9 GB of its 64 GB at the time, so the figures are if anything pessimistic —
a bandwidth measurement does not need a free heap, only a working set that
dwarfs the 34 MB L2, and stating the contention is better than quietly omitting
it.

Read exceeds copy and triad because a read-only stream has no write traffic to
interleave; write-only is lowest because it gets no benefit from read-allocate.
That ordering is the expected shape. A card that inverts it is interesting.

### Interpreting a low figure

80–90 % of derived peak is what a tuned STREAM kernel achieves on GA100-class
HBM2e. Below ~70 % on an idle GPU, suspect the card rather than the benchmark:

- **`clocks.mem` below `clocks.max.mem` under load** — the memory is
  downclocked; the tool prints both and flags it.
- **Another process on the GPU** — reported, not guessed, from
  `nvidia-smi --query-compute-apps`.
- **A card not running its unlocked geometry** — a memory clock of ~1458 MHz
  rather than 1728 means the unlock did not take, or a kernel upgrade reverted
  it (see [bench-setup](../bench-setup/README.md#3-unlock)).

Throughput is deliberately a **separate tool**, not a stage of the screen: it
needs PyTorch with CUDA, and the screen must keep running on a bare bench with
nothing but the driver, `vulkan-tools` and Python's standard library.

## 6. The batch is the reference

Because the card is passive, absolute temperature depends on your bench airflow.
There is no universal "too hot" threshold to quote.

What is meaningful is the **batch median °C/W**, with cards more than **20 %
over the median** flagged for cooling service. `--rollup` computes this, and
declines to compute it from fewer than three cards.

This only works if the batch was screened **the same way**:

> **Parallel and solo runs are not comparable.** Cards run together heat each
> other. Screen a whole batch in one mode, or the median-vs-outlier method is
> meaningless.

Same reason the bench wants **fixed** airflow rather than a BIOS fan curve — see
[bench-setup](../bench-setup/README.md#1-the-physical-bench).

## 7. Kernel errors: only this run counts

The screen snapshots the `dmesg` line count *before* the test and greps only the
lines added during it.

Grepping the whole ring buffer flagged our reference card as suspect on the
strength of Xids from a PCIe experiment weeks earlier — a false alarm that would
fire on essentially every used card. Historical entries are still captured, but
as clearly separated context, never as this run's result.

## 8. Multi-card benches

Everything in this section exists because the naive version **mis-attributed
results rather than erroring** — the most dangerous class of bug in a test rig,
because the output still looks like a clean pass.

- **The test tool's device order does not match `nvidia-smi`.** On our bench its
  device 1 = PCI 06:00 = smi GPU 1, while its device 2 = PCI 05:00 = smi GPU 0.
  Left on its default device, the report prints one card's serial above another
  card's result. We match on **PCI address**, which both tools agree on, and
  refuse to run if the mapping cannot be established.
- **Passing an explicit device makes the binary a worker process**, and the
  worker does not print the parent's closing `testing PASSed` line when stopped
  at the time limit — so a clean run read as FAIL. The verdict is now derived
  from error markers plus exit code 124 (proof that *our* time limit ended it).
- **In parallel mode the device map is resolved once, in the launcher.**
  Enumerating inside each child would briefly start the tool on its default
  device — i.e. on a card another child is mid-test on.
- Summing the tool's `Passed N seconds` lines is **not** a coverage measure. It
  reports every 30 s, so any run is undercounted by up to a full interval.

### Why `INCONCLUSIVE` is not a pass

A pass requires **both** no error markers **and** proof the test ran the whole
window. Anything else is `INCONCLUSIVE`, never `PASS`. The temptation to collapse
those two into "no errors seen, ship it" is exactly how a card that crashed 90
seconds in gets sold as tested.

## 9. What a PASS does not mean

- Not that the card is error-free. It means **no errors were detected across the
  full heap, at operating temperature, over the stated duration**. Intermittent
  faults evade any finite test.
- Not that the memory is ECC-protected. It is not, and cannot be.
- Not that the card will run this cool in your chassis. Thermal figures describe
  the card *in our bench's airflow*.
- Not that every memory channel is enabled — see §1. Full heap present and
  addressable, throughput in band.
- Not a statement about speed at all. Throughput is a **separate** measurement
  (§5b) and is deliberately not folded into the verdict: a card can be perfectly
  sound and still be downclocked, and collapsing the two would hide that.

## 10. Evidence

Every run keeps its raw material: full `nvidia-smi -q`, the identity CSV,
`vulkaninfo`, the complete memory-test output, the per-sample telemetry CSV, the
kernel log slice, and **`SHA256SUMS` over all of it**. The markdown report is a
summary of those files, not a substitute for them. A buyer who wants to check
our arithmetic has the curve.
