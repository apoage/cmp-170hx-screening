#!/usr/bin/env python3
"""VRAM verdict with error granularity — PASS / PASS-ECC / MASK-CANDIDATE / FAIL.

Reads a memtest_vulkan run log (as captured by hx-vram-screen.sh) plus
optional nvidia-smi -q snapshots from before and after the run, and classifies:

  PASS            no errors detected over proven coverage (>= window - 90 s
                  of recorded test time; less reads as INCONCLUSIVE, frozen)
  PASS-ECC        pattern-clean, but ECC corrected events appeared during the
                  run — disclosed as a finding, never as a sales note
  MASK-CANDIDATE  errors whose BUFFER addresses sit high enough that a smaller
                  unlock might exclude them. NOT a pass: buffer position is
                  not physical position (HBM is interleaved), so the card
                  FAILS until a retest at the smaller geometry passes
  FAIL            errors below the smallest geometry, uncorrectable ECC
                  events, or error markers this tool could not fully parse —
                  unparsed errors never downgrade to PASS
  INCONCLUSIVE    neither proven coverage nor errors — rerun

Error addresses come from memtest_vulkan's own reporting:
  "Error found. Mode <m>, total errors 0xN out of 0xN (P%)"
  "Errors address range: 0xSTART..=0xEND"
Addresses are offsets into the TESTED region, so the lowest error address
directly bounds the unconditionally-usable memory.

Usage:
  hx-vram-verdict.py LOG TESTED_BYTES [--rc N] [--pre-q FILE] [--post-q FILE]
                     [--min-usable-gib N] [--md]
Exit code: 0 = PASS*, 1 = FAIL, 2 = INCONCLUSIVE.
"""
import argparse
import re
import sys

GIB = 1 << 30
ERROR_LINE = re.compile(r"Error found\. Mode (\S+), total errors 0x([0-9A-Fa-f]+)")
RANGE_LINE = re.compile(r"Errors address range:\s*0x([0-9A-Fa-f]+)\.\.=0x([0-9A-Fa-f]+)")
# broad backstop: ANY error wording — an unparsed error must never pass
ERROR_MARKER = re.compile(r"(?i)(errors? found|total errors|error detected)")
PASS_LINE = re.compile(r"no any errors, testing PASSed")
COVERED_LINE = re.compile(r"Passed +([0-9.]+) seconds")
ECC_CORR = re.compile(r"(Correctable|Corrected)[^:]*:\s*(\d+)", re.I)
ECC_UNCORR = re.compile(r"(Uncorrectable|Une)[^:]*:\s*(\d+)", re.I)
# 170HX unlock profiles: the smallest standard re-unlock is 40 GiB, so a
# maskable verdict must leave at least that much unconditionally usable.
DEFAULT_MIN_USABLE_GIB = 40


def ecc_totals(path):
    """(corrected, uncorrectable) VOLATILE counters from an nvidia-smi -q
    snapshot, or None when the card doesn't expose ECC counters.

    Volatile only: the Aggregate block is lifetime-cumulative and summing
    both would roughly double the run delta. "N/A" fields mean the counter
    set is absent on this card — that reads as not-readable, not as zero."""
    try:
        text = open(path, errors="replace").read()
    except OSError:
        return None
    volatile = re.split(r"(?im)^\s*Aggregate\s*$", text)[0]
    if not re.search(r"(?i)correct", volatile):     # no ECC section at all
        return None
    if re.search(r"(?i)(correctable|une)[^:\n]*:\s*N/A", volatile):
        return None
    # (?<!Un): "Correctable" must not match inside "Uncorrectable"
    corr = sum(int(m.group(2)) for m in
               re.finditer(r"(?i)(?<!Un)(Correctable|Corrected)[^:]*:\s*(\d+)",
                           volatile))
    unc = sum(int(m.group(2)) for m in
              re.finditer(r"(?i)(Uncorrectable|Une)[^:]*:\s*(\d+)", volatile))
    return corr, unc


def classify(log, tested_bytes, rc, pre_q, post_q, min_usable_gib, mins):
    # window minus 90 s slack: startup plus the unreported tail of the last
    # 30 s interval cost a real 15-min run ~50 s of recorded time (our own
    # PASS example recorded 848 s of 900). 45 s of slack broke every real
    # run; 90 s keeps the freeze protection (a run frozen at 60 s still
    # reads INCONCLUSIVE).
    expected_min = mins * 60 - 90
    errors = []          # (mode, count, start, end)
    error_markers = 0    # ANY error wording — backstop: unparsed ≠ pass
    clean_pass = False
    covered = 0
    pending = None
    for line in log.splitlines():
        if ERROR_MARKER.search(line):
            error_markers += 1
        m = ERROR_LINE.search(line)
        if m:
            pending = (m.group(1), int(m.group(2), 16))
            continue
        m = RANGE_LINE.search(line)
        if m and pending:
            start, end = int(m.group(1), 16), int(m.group(2), 16)
            if start >= tested_bytes or end >= tested_bytes or end < start:
                # impossible address: treat like an unparsed error, never
                # let a malformed range downgrade the verdict
                errors.append(None)
            else:
                errors.append((pending[0], pending[1], start, end))
            pending = None
            continue
        if PASS_LINE.search(line):
            clean_pass = True
        m = COVERED_LINE.search(line)
        if m:
            covered += int(float(m.group(1)))

    # Backstop: error wording in the log that we could not fully parse
    # (missing range line, different wording, device-lost banners) must
    # never read as PASS.
    unparsed = error_markers - len(errors)
    if unparsed > 0 or None in errors:
        return ("FAIL", [
            "error markers in log: %d, fully parsed: %d — refusing to grade "
            "unparsed errors as anything but FAIL" %
            (error_markers, len(errors)),
            "inspect the raw memtest log; if the card reported errors in a "
            "format this tool does not parse, that is a failing run"])

    # ECC deltas across the run, when the card exposes counters at all.
    # The note is ALWAYS set — a None note crashed --md and printed a
    # literal '- None' line into buyer reports.
    corr_delta = unc_delta = 0
    ecc_readable = False
    pre, post = ecc_totals(pre_q) if pre_q else None, ecc_totals(post_q) if post_q else None
    if pre is not None and post is not None:
        ecc_readable = True
        corr_delta, unc_delta = post[0] - pre[0], post[1] - pre[1]
        if unc_delta > 0:
            ecc_note = "ECC uncorrectable events during run: +%d" % unc_delta
        elif corr_delta > 0:
            ecc_note = ("ECC corrected (single-bit) events during run: +%d — "
                        "disclosed as a finding; corrected errors on a resale "
                        "card are a fact to weigh, not a pass note" % corr_delta)
        else:
            ecc_note = "ECC counters readable: no corrected or uncorrectable events during run"
    else:
        ecc_note = ("ECC counters not readable on this card (absent or N/A) — "
                    "pattern testing is the only instrument")

    if errors:
        lowest = min(e[2] for e in errors)
        total = sum(e[1] for e in errors)
        usable_gib = lowest // GIB          # everything below this is clean
        heap_gib = tested_bytes // GIB
        if unc_delta > 0:
            return ("FAIL", [
                "errors: %d in %d range(s), lowest at %d/%d GiB" %
                (total, len(errors), lowest // GIB, heap_gib), ecc_note,
                "FAIL — uncorrectable ECC events during the run (not fixable)"])
        if usable_gib >= min_usable_gib:
            # NB: the address is an offset in memtest_vulkan's BUFFER, not a
            # physical address. GPU memory is interleaved across HBM channels
            # and a smaller unlock changes which physical partitions are
            # used — it does NOT simply crop the top of the buffer. So this
            # is a CANDIDATE for rescue-by-retest, not a pass: the card
            # fails until a retest at the smaller geometry actually passes.
            return ("MASK-CANDIDATE", [
                "errors: %d in %d range(s), lowest at %d of %d GiB in the "
                "tested buffer" % (total, len(errors), usable_gib, heap_gib),
                ecc_note,
                "FAIL — mask candidate (unverified): buffer addresses suggest "
                "the errors sit high, but buffer position is not physical "
                "position; re-unlock at <= %d GiB and RETEST. Only a passing "
                "retest at the smaller geometry changes this verdict" %
                usable_gib])
        return ("FAIL", [
            "errors: %d in %d range(s), lowest at %d/%d GiB" %
            (total, len(errors), lowest // GIB, heap_gib), ecc_note,
            "FAIL — errors reach into the always-sellable region (below the "
            "%d GiB minimum geometry); not fixable by masking" % min_usable_gib])

    if (clean_pass or rc == 124) and covered >= expected_min:
        if unc_delta > 0:
            return ("FAIL", [ecc_note,
                             "FAIL — pattern-clean but uncorrectable ECC "
                             "events occurred during the run (not fixable)"])
        if corr_delta > 0:
            return ("PASS-ECC", [ecc_note,
                                  "PASS — ECC-corrected events during the run "
                                  "are disclosed above as a finding"])
        return ("PASS", [ecc_note])
    if rc == 124 and 0 < covered < expected_min:
        return ("INCONCLUSIVE", [
            "coverage %ds of an expected >=%ds window (rc=124): the test "
            "stopped reporting early — possibly frozen or the card dropped "
            "off the bus. Never read as a pass; rerun" % (covered, expected_min)])
    return ("INCONCLUSIVE", [
        "no proven coverage (rc=%s, %ds reported) — rerun" %
        (rc, covered)])


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("log")
    ap.add_argument("tested_bytes", type=lambda x: int(x, 0))
    ap.add_argument("--rc", type=int, default=1,
                    help="memtest exit code (124 = time limit = full window)")
    ap.add_argument("--mins", type=int, default=15,
                    help="test window in minutes; the coverage floor is "
                         "window minus one report interval")
    ap.add_argument("--pre-q")
    ap.add_argument("--post-q")
    ap.add_argument("--min-usable-gib", type=int, default=DEFAULT_MIN_USABLE_GIB)
    ap.add_argument("--md", action="store_true", help="emit a markdown block")
    a = ap.parse_args()

    verdict, detail = classify(open(a.log, errors="replace").read(),
                               a.tested_bytes, a.rc, a.pre_q, a.post_q,
                               a.min_usable_gib, a.mins)
    if a.md:
        print("| | |")
        print("|---|---|")
        for d in detail:
            print("| | %s |" % d.replace("|", "\\|"))
    else:
        print(verdict)
        for d in detail:
            print(d)
    sys.exit({"FAIL": 1, "MASK-CANDIDATE": 1, "INCONCLUSIVE": 2}.get(verdict, 0))


if __name__ == "__main__":
    sys.exit(main())
