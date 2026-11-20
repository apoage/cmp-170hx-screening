# Changelog

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
