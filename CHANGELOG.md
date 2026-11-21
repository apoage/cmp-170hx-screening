# Changelog

## 1.6.3 — 2026-11-20

No-device path hardened (fifth review — found live on a rev-ff wedged card).

- nvidia-smi prints failure PROSE ("No devices were found", "Unable to
  determine the device handle…") on STDOUT, sometimes with exit 0, so the
  old `[ -n ]` checks accepted it as data: the banner read
  `serial Nodeviceswerefound` and the idle guard aborted with "GPU not idle
  (No devices were found MiB used)". Every guard read now requires exit 0
  AND the right shape (serial = digits, UUID = `GPU-…`, MiB = numeric);
  `--query-compute-apps` output only counts as apps when it has commas.
- New `gpu_triage` (exit 4): when no usable device, diagnose from the PCI
  bus with the same 10de:20b0/20c2/2082 test the unlock installer uses —
  no card fitted vs. `(rev ff)` fall-off-bus wedge (Xid 79; ~60 s cold
  power cut — one warm-reboot recovery observed 2026-11-20, noted in the
  message but not relied on) vs. on the bus with no driver handle.
- `cardreport.py`: a card on the bus but not enumerated by nvidia-smi is
  now flagged in the dossier ("?" = UNKNOWN, not zero, with the reason)
  instead of silently printing bare unknowns.
- README: exit-code contract now documents 3, 4 and 64 alongside 0/1/2.
- FATAL, pre-existing at 1.6.2: the report heredoc referenced `${PCI_FULL}`,
  a variable assigned nowhere — only `PCI_FULL_ID` exists, and only in the
  `--identify` path. Under `set -u` the `cat > $REPORT` died mid-write
  (rc=127) while the script continued to a normal exit, so every full
  screen silently produced NO acceptance report while the FLEET row and the
  mid-run dossier write made the run look complete. Found live 2026-11-20
  (run 1322421002720_20261120-184101). `PCI_FULL` is now derived from
  `BUSID` before the heredoc, with a `:-` fallback in the header line.

## 1.6.2 — 2026-11-20

Guard logic fix (fourth review): a CLEAN module always read "not checked" —
`grep -c` prints 0 but exits 1 on zero matches, so the read was treated as
failed, and the srcversion cross-check was unreachable. The guard now lives
in `bin/hx-module-check.sh` (callable standalone, explicit-path mode for
tests): decompressor-checked first, `|| :` on the count, "ok" on clean,
exit 3 on marker, srcversion comparison actually reachable. Dropped the
`strings` dependency — `grep -a` searches binary data directly. CI runs the
five-case fake-module matrix (clean/marked, plain/gz, unreadable) so the
exit-code semantics cannot regress.

## 1.6.1 — 2026-11-20

Attribution and robustness corrections.

- 0010-sec2-hs-fusegate-probe was OUR local experimental patch, never part
  of cmpunlocker (our own postmortem: created locally 2026-08-31, untracked,
  never upstreamed). The docs and the guard comment wrongly credited it to
  cmpunlocker and then said its patch corrupts RAM — corrected here and in
  the docs; earlier entries below still carry the wrong wording and are
  superseded by this note. ("Verified against the full v0.4 series" meant
  the marker string is unique versus the whole upstream v0.4 patch set.)
- The guard now unpacks compressed DKMS modules (.ko.zst/.ko.xz/.ko.gz —
  plain `strings` on a compressed file finds nothing and silently passed),
  cross-checks the loaded module's srcversion against the on-disk file, and
  prints "module provenance: not checked" when it cannot read the module.
- bench-setup: removed the false "the installer checks its exit code" claim.
- Fixture scrub finished: GPU PDI (per-chip unique) redacted, Processes
  block removed. The un-scrubbed v1.5.0 capture remains in git history
  (343fcc4) — the serial is published deliberately elsewhere (examples/),
  so history was left alone.
- README: dashboard JSON endpoints corrected to /api/status and /api/live.

## 1.6.0 — 2026-11-20

Third review round.

- The fusegate guard the docs promised now exists in the code: the screen
  inspects the loaded nvidia module for the patch-0010
  `sec2-hs-fusegate-probe` marker string and hard-stops (the check our
  internal preflight always had, now shipped).
- Coverage floor gets a 30 s minimum: `mins*60-90` goes negative below
  2-minute windows, so `hx 1` could PASS with zero recorded time. Now
  `max(window-90, 30)`; the bash fallback uses the same floor (was still
  -45). CI case added.
- Fatal-Xid list drops 13/31/43 (often application-caused; still flagged
  via the normal kernel-error path). Bench-verified first: a TERM-killed
  memtest logs no Xid at all, so the kill path cannot spray false fatals.
- memtest version grep matches the real `v`-prefixed banner.
- Verdict docstring no longer claims error addresses bound usable memory.
- Remaining "full heap" phrasing replaced with whole-free-heap / tested-heap
  (~97 % of free) everywhere.
- Real N/A-ECC fixture scrubbed of the donor card's serial/UUID (the ECC
  lines are all the tests need); README gains an "Accessing the dashboard"
  section (local URL, LAN bind, ssh -L tunnel, TUI alternative).

## 1.5.1 — 2026-11-20

README rewritten as a technical reference: measurement/instrument focus,
usage + env-var tables, outputs and exit codes up front; sales narrative
removed repo-wide (README, examples note, script comments, verdict wording).

## 1.5.0 — 2026-11-20

Second review round: the grader itself was over-confident.

**Fixed — regressions in the verdict path:**
- Real 15-minute runs recorded ~848 s of 900 and read INCONCLUSIVE under the
  45 s slack. The coverage floor is now window − 90 s (our own PASS example
  passes; a run frozen at 60 s still reads INCONCLUSIVE).
- Unparsed error markers (different wording, device-lost banners, missing
  range lines, impossible addresses) can no longer read as PASS: any error
  wording that is not fully parsed grades FAIL.
- ECC note was None on the most common path (counters readable, delta
  zero) — crashed `--md`, printed `- None` into buyer reports. The note is
  always set; N/A counters count as not-readable, not as zero.
- Xid matching rewrote to the real line format `NVRM: Xid (PCI:0000:05:00):
  79` — bus:device with no `.function`: fixed-string PCI tag (dots are not
  regex), fatal code read after the closing paren (was reading `0000` out
  of the address). The fatal-Xid FAIL can actually trigger now — which
  matters with ECC on, since 48/63/64/94/95 are the ECC events. Same fix in
  cardreport's per-card Xid count. CI carries a real-format Xid fixture.
**Design change — MASK-CANDIDATE is a FAIL, not a pass:** buffer addresses
  are not physical addresses (HBM is interleaved; a smaller unlock changes
  which partitions are used, it does not crop the buffer). A card whose
  errors sit high now FAILS with rescue instructions (re-unlock smaller,
  retest) until that retest passes.
**Also:** Volatile-only ECC parsing (Aggregate double-count fixed),
  `(?<!Un)Correctable` no longer matches inside Uncorrectable, PASS-ECC
  wording drops the sales claim and just discloses the count, bench-setup
  IOMMU wording unmuddled + AMD note + driver-pairing + fusegate caveat,
  README documents the dashboard's deliberate LAN bind under --service.

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
