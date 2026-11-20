# Changelog

## 1.4.0 — 2026-11-20

`--identify` / `hx --identify`: the report of what a card **is**, without any
test claims. Identity + provenance + full card dossier (physical silicon,
driver-recorded hours) + independent Vulkan heap + per-card kernel-log
history, written to `<SERIAL>-identity.md`. Deliberately separate from the
acceptance report: it never overwrites a test report, never writes a FLEET
row, and its status line reads **IDENTIFIED — NOT TESTED** with a pointer to
`hx 15`. Works with `--gpu N` and under `--all` for whole batches.

## 1.3.1 — 2026-11-20

Two claim corrections from bench practice:

- bench-setup is **working-first**: use current upstream cmpunlocker rather
  than pre-emptively pinning; pinning is the rollback tool, not the default.
- ECC story updated everywhere: stock 170HX ships ECC fused off, but recent
  cmpunlocker (v0.5+) **enables DRAM+SRAM ECC** — the screen reads the
  counter delta across every soak on such cards (PASS-ECC/FAIL grading);
  pattern testing remains the instrument only when ECC is absent. Post-unlock
  verification now includes the ECC mode check.

## 1.3.0 — 2026-11-20

External review round: the tool must never print something more confident
than the evidence. (Also corrects the 1.0.0 entry below: reports are
**hash-sealed** via `SHA256SUMS`, not cryptographically signed.)

**Fixed — could mislead:**
- A frozen test run can no longer read as PASS: the verdict requires recorded
  coverage of at least the window minus one 30 s report interval (+slack),
  and the soak uses `timeout -k 30` so a TERM-ignoring wedge cannot hang the
  bench. "Crashed 90 s in, sold as tested" is now INCONCLUSIVE.
- Kernel log: `dmesg` → `sudo -n dmesg` fallback; unreadable logs report
  **"not measured"**, never a silent 0; errors are attributed to the card's
  own PCI address/UUID (no cross-attribution on `--parallel` benches); fatal
  Xid classes block any PASS-prefixed verdict.
- The dossier is scoped to the card under test (`--pci-addr`) — no longer
  leaks the other cards' serials/UUIDs on multi-card benches.
- Lifetime hours described truthfully: driver-recorded hours since the
  InfoROM was last initialised; a wiped InfoROM restarts the counter, so low
  hours on a wiped card mean unknown. NVML `_v1` call provenance documented.
- Exit codes are a contract: PASS-family 0, FAIL 1, INCONCLUSIVE 2.
- `cardreport --ffrows`: missing `import time` (crashed after first row).
- Dashboard inline markdown: whole-word underscores only — fuse-word names
  no longer render as italic garbage.

**Improved:**
- status/fuse words of 0 are valid readings (0 = "nothing disabled", not
  "unreadable"); `words_raw` keeps zeros; `0x2082` accepted; GPC
  enabled>latent flagged as inconsistent; per-card Xid counting.
- Report embeds the FLEET.csv run history for the serial (a rerun PASS
  cannot erase an earlier FAIL), inline miscompare lines on FAIL/MASK,
  actual memtest binary version+hash, per-card kernel-log section (§5b),
  honest coverage phrasing ("61 GiB of 62 free, ~97 % of free").
- SHA256SUMS written with relative paths.
- Dashboard defaults to 127.0.0.1 (`HX_BIND` to expose; systemd template
  documents the deliberate 0.0.0.0 opt-in). `MINS >= 1` enforced.
- bench-setup: pin the validated cmpunlocker commit, IOMMU warning,
  container-toolkit purge note, BIOS-only secure-boot handling, installer
  exit code checked, dashboard section numbering.
- CI: frozen / early-exit / no-closing-line fixtures cover the verdict logic.

## 1.2.0 — 2026-11-20

VRAM errors are now graded, not just counted — and the unlock is a hard
precondition.

- `bin/hx-vram-verdict.py` — granular VRAM verdict from memtest_vulkan's own
  error addresses plus the ECC counter delta across the run:
  `PASS` (clean) / `PASS-ECC` (corrected single-bit errors during the run —
  fixable, disclosed) / `PASS-MASK` (every error address above a boundary a
  smaller unlock geometry excludes — reducible-geometry card, report states
  the usable GiB) / `FAIL` (errors below the 40 GiB minimum geometry or
  uncorrectable ECC events — not fixable). `INCONCLUSIVE` unchanged. Exit
  code mirrors the verdict; fixture-tested in CI.
- `hx-vram-screen.sh` captures `nvidia-smi -q` before/after the soak for the
  ECC delta, embeds the verdict detail in report §4, and the report's "no
  ECC" scope note is now conditional on what the card actually exposes.
- Unlock-first gate: the screen aborts on a locked card (under 16 GiB total)
  with instructions to install CMPUnlocker first (bench-setup/), cold boot,
  and rerun. Quick start leads with step 0.

## 1.1.0 — 2026-11-20

Card dossier: the report now tells you what the silicon is worth, not just
that it works.

- `bin/cardreport.py` — read-only card dossier, stdlib-only: physical VRAM
  from the FBP defect words (latent GiB + per-HBM-stack table, 96 GiB full
  bin), latent vs enabled SMs per GPC, NVML BBX lifetime power-on hours,
  NVLink / MIG-ECC-NVDEC / PCIe generation disable words, SBFE state.
  Non-GA100 NVIDIA cards are listed but skipped. `--selftest` validates the
  analytics offline against a reference card; `--ffrows` (idle card) walks
  the physical fuse rows for FF burn evidence and remaining correction
  budget. Register addresses from public GA100 research (kate-land, live
  confirmed by honzapatCZ/cmp170hx-test); no upstream code.
- `hx-vram-screen.sh` captures the dossier at step [1a] of every screen,
  logs a one-line summary, and appends the full dossier to the per-card
  report — buyers see latent capacity next to the pass/fail. Degrades to
  identity + lifetime hours without root; the screen never fails because of it.
- `install.sh` runs the dossier selftest at install time.

## 1.0.0 — 2026-08-31

First public release. The suite as used to screen our own resale batches.

- `hx-vram-screen.sh` — per-card screen: idle guard, identity and provenance
  classification, independent Vulkan heap enumeration, full-heap memory test
  doubling as the thermal soak, kernel-log slice, signed report.
- `hx-thermal-analyze.py` — °C/W triage for core and HBM, repaste vs repad.
- `hx-bandwidth.py` — STREAM-style HBM2e throughput against a peak derived from
  the card's own bus width and memory clock. Measured 1650 GB/s (93 % of
  derived peak) on an unlocked card, above the 1493 GB/s published figure; the
  same arithmetic shows the unlock lifts the memory clock +18.5 % and leaves the
  bus width unchanged.
- `hx-dashboard.py` — stdlib-only read-only web UI, batch index, live run,
  customer-ready export.
- `hx-tui.py` — console display for a headless bench.
- `--all`, `--parallel` and `--rollup` for batch work.
- `bench-setup/` — driver, unlock sequencing (calls the third-party
  [cmpunlocker](https://github.com/amoghmunikote/cmpunlocker)), airflow and fan
  wiring notes.
- `docs/METHODOLOGY.md` and real `examples/`.
