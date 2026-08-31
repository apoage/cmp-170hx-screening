# CMP 170HX — VRAM & thermal acceptance report

**GPU serial:** 1322621098890
**GPU UUID:** GPU-b638ea07-3919-864b-b7bd-4f43a21a1ab3   ·   **slot:** PCI 05:00
**Date:** 2026-08-28 08:04 UTC
**Verdict:** **PASS**

## 1. Identity
```
name, serial, uuid, vbios_version, pci.bus_id, memory.total [MiB], driver_version
NVIDIA CMP 170HX, 1322621098890, GPU-b638ea07-3919-864b-b7bd-4f43a21a1ab3, 92.00.6D.00.0A, 00000000:05:00.0, 65536 MiB, 610.43.03
```

## 2. Provenance — **DOCUMENTED**

| Field | Value |
|---|---|
| Serial number | 1322621098890 |
| Board part number | 900-11001-0108-000 |
| VBIOS | 92.00.6D.00.0A |
| InfoROM image | 1001.0108.01.02 |

The card's onboard inventory is intact and self-consistent: it reports its own
serial number, board part number and InfoROM version, and identifies itself as a
CMP 170HX. Every result below is tied to the serial above.

## 3. Memory size confirmed by an independent, non-vendor API
Read through **Khronos Vulkan**, a driver path independent of NVIDIA's own tools.

| Source | Device-local memory |
|---|---|
| NVIDIA (nvidia-smi) | 65536 MiB |
| Vulkan (vendor-neutral) | 64.00 GiB |

## 4. VRAM integrity — **PASS**
| | |
|---|---|
| Instrument | memtest_vulkan v0.5.0 (open source, Khronos Vulkan) |
| Duration | 15 min continuous (848s of testing recorded) |
| Coverage | 61 GiB tested of 65053 MiB free, on an idle GPU |
| Card under test | PCI 05:00 (matched to test-tool device 2 by PCI address) |
| Peak throughput | 937.6GB/sec |
| Result | **PASS** |

The GPU was verified idle before the run. Had any process held memory, the test
would have covered only the free remainder; the run is blocked in that condition
precisely so a partial pass cannot be mistaken for a full one.

### Thermal curve

Run length 14.9 min under load, sampled every 2 s, card held at **242 W** throughout. Every card runs the identical workload, so the temperature reached is a direct measure of heat transfer.

```
core 37-65C  ▁▃▄▆▆▇▇▇▇███████████▆▆▇▇████████████████████████████████████
HBM  52-74C  ▁▃▄▅▆▆▇▇▇▇▇█▇███████▅▆▆▇▇█▇▇████████████████████████████████
```

| Metric | Core | HBM | Reading |
|---|---|---|---|
| Idle baseline | 31 C | 45 C | gap 14 C |
| Plateau (last 25%) | 65 C | 73 C | gap 8 C |
| Rise over idle | +34 C | +27 C | at 242 W |
| Thermal resistance | 0.139 C/W | 0.113 C/W | lower = better contact |
| End-of-run slope | -0.2 C/min | -0.2 C/min | >+2 = not plateaued |
| Peak | 65 C | 74 C | |
| Throttle reasons | SW power cap (expected under sustained load) | | |

**Thermal verdict: OK**

- Plateaued cleanly, no thermal throttling, temperatures within band.

_Comparison note: this card is passively cooled (no onboard fan sensor), so absolute temperatures depend on bench airflow. Screen the whole batch on the same bench and compare C/W across cards — the batch median is the reference, and outliers are the repaste candidates._

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


## 7. Evidence
Raw logs in `1322621098890_20260828-094851/`, hashed in `SHA256SUMS`. Telemetry is the
full per-sample curve in `03-telemetry.csv`.
