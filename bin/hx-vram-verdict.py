#!/usr/bin/env python3
"""VRAM verdict with error granularity — PASS / PASS-ECC / PASS-MASK / FAIL.

Reads a memtest_vulkan run log (as captured by hx-vram-screen.sh) plus
optional nvidia-smi -q snapshots from before and after the run, and classifies:

  PASS        no errors detected over proven coverage
  PASS-ECC    pattern-clean, but ECC corrected errors appeared during the run
              (weak cells the ECC repairs — fixable, disclosed)
  PASS-MASK   errors exist but every error address sits at or above a heap
              boundary that a smaller unlock geometry can exclude — a
              reducible-geometry card, sellable at the lower capacity after
              re-unlock + retest
  FAIL        errors inside the always-sellable region, or scattered (not
              fixable by any geometry)
  INCONCLUSIVE  neither a pass nor errors over proven coverage — rerun

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
PASS_LINE = re.compile(r"no any errors, testing PASSed")
COVERED_LINE = re.compile(r"Passed +([0-9.]+) seconds")
ECC_CORR = re.compile(r"(Correctable|Corrected)[^:]*:\s*(\d+)", re.I)
ECC_UNCORR = re.compile(r"(Uncorrectable|Une)[^:]*:\s*(\d+)", re.I)
# 170HX unlock profiles: the smallest standard re-unlock is 40 GiB, so a
# maskable verdict must leave at least that much unconditionally usable.
DEFAULT_MIN_USABLE_GIB = 40


def ecc_totals(path):
    """(corrected, uncorrectable) summed over an nvidia-smi -q snapshot."""
    try:
        text = open(path, errors="replace").read()
    except OSError:
        return None
    corr = sum(int(m.group(2)) for m in ECC_CORR.finditer(text))
    unc = sum(int(m.group(2)) for m in ECC_UNCORR.finditer(text))
    return corr, unc


def classify(log, tested_bytes, rc, pre_q, post_q, min_usable_gib, mins):
    expected_min = mins * 60 - 45          # window minus one 30 s report
                                           # interval and slack: memtest_vulkan
                                           # reports every 30 s, so a run that
                                           # truly covered the window has at
                                           # least this much recorded time
    errors = []          # (mode, count, start, end)
    clean_pass = False
    covered = 0
    pending = None
    for line in log.splitlines():
        m = ERROR_LINE.search(line)
        if m:
            pending = (m.group(1), int(m.group(2), 16))
            continue
        m = RANGE_LINE.search(line)
        if m and pending:
            errors.append((pending[0], pending[1],
                           int(m.group(1), 16), int(m.group(2), 16)))
            pending = None
            continue
        if PASS_LINE.search(line):
            clean_pass = True
        m = COVERED_LINE.search(line)
        if m:
            covered += int(float(m.group(1)))

    # ECC deltas across the run, when the card exposes counters at all
    ecc_note = None
    corr_delta = unc_delta = 0
    pre, post = ecc_totals(pre_q) if pre_q else None, ecc_totals(post_q) if post_q else None
    if pre and post:
        corr_delta, unc_delta = post[0] - pre[0], post[1] - pre[1]
        if unc_delta > 0:
            ecc_note = "ECC UNCORRECTABLE errors during run: +%d" % unc_delta
        elif corr_delta > 0:
            ecc_note = ("ECC corrected (fixable) errors during run: +%d — "
                        "weak cells the ECC repairs; disclosed, not a defect "
                        "for inference duty" % corr_delta)
    else:
        ecc_note = ("ECC counters not readable on this card — pattern testing "
                    "is the only instrument (this SKU ships ECC fused off "
                    "unless the unlock enables it)")

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
            return ("PASS-MASK", [
                "errors: %d in %d range(s), all at or above %d GiB of the %d GiB tested"
                % (total, len(errors), usable_gib, heap_gib), ecc_note,
                "PASS (mask region): the lowest %d GiB is clean — re-unlock "
                "with a smaller geometry (profile leaving <= %d GiB), retest, "
                "and sell as a %d GiB card" %
                (usable_gib, usable_gib, usable_gib)])
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
                                  "PASS — with disclosed ECC-corrected "
                                  "(fixable) errors during the run"])
        return ("PASS", [ecc_note if pre and post else
                         "ECC counters not readable — pattern testing only"])
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
    sys.exit({"FAIL": 1, "INCONCLUSIVE": 2}.get(verdict, 0))


if __name__ == "__main__":
    sys.exit(main())
