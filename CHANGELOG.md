# Changelog

## 1.0.0 — 2026-08-31

First public release. The suite as used to screen our own resale batches.

- `hx-vram-screen.sh` — per-card screen: idle guard, identity and provenance
  classification, independent Vulkan heap enumeration, full-heap memory test
  doubling as the thermal soak, kernel-log slice, signed report.
- `hx-thermal-analyze.py` — °C/W triage for core and HBM, repaste vs repad.
- `hx-dashboard.py` — stdlib-only read-only web UI, batch index, live run,
  customer-ready export.
- `hx-tui.py` — console display for a headless bench.
- `--all`, `--parallel` and `--rollup` for batch work.
- `bench-setup/` — driver, unlock sequencing (calls the third-party
  [cmpunlocker](https://github.com/amoghmunikote/cmpunlocker)), airflow and fan
  wiring notes.
- `docs/METHODOLOGY.md` and real `examples/`.
