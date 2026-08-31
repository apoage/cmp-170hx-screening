#!/usr/bin/env python3
"""CMP 170HX — HBM2e memory throughput measurement (STREAM-style).

WHAT THIS IS FOR
  The screening report proves the memory is *sound*. This proves it is *fast*,
  which is the other half of what a buyer is paying for: a 170HX whose heap
  passes but whose HBM runs at a fraction of spec is a different card from the
  one in the listing.

WHY NOT JUST QUOTE memtest_vulkan
  The screen already prints a GB/sec figure (~940 GB/s on our cards), but that
  number is a floor, not a bandwidth measurement: memtest_vulkan generates and
  verifies patterns while it streams, so ALU work and the read-back are folded
  into the same figure. A STREAM kernel does nothing but move bytes, so it
  measures the memory system rather than the test harness.

THEORETICAL PEAK IS DERIVED, NOT LOOKED UP
  bus width x memory clock x 2 (DDR), both read from the device itself:
      4096 bit x 1.728 GHz x 2 / 8 = 1769 GB/s
  Note this is ABOVE the 1493 GB/s published CMP 170HX figure -- the marketing
  number describes the card in its restricted state. We report the derived peak
  and the measured fraction of it, so the claim carries its own arithmetic.

WHAT COUNTS AS GOOD
  A well-tuned STREAM kernel on GA100-class HBM2e lands at 80-90% of derived
  peak. Below ~70% on an idle GPU points at downclocked memory (check
  clocks.mem against clocks.max.mem) or a card not running at its unlocked
  geometry -- not at this benchmark.

Usage:
  python3 hx-bandwidth.py [--gpu N] [--size-mib N] [--iters N] [--json]

Needs PyTorch with CUDA. On a screening bench that is usually an inference
venv: ~/vllm-env/bin/python hx-bandwidth.py
"""
import argparse
import json
import subprocess
import sys

BYTES_PER_GB = 1e9          # decimal GB, as vendors quote bandwidth


def smi(fields, gpu):
    """Query nvidia-smi, returning a dict. Never fatal: the benchmark result is
    still valid without the telemetry, and this must not be the thing that
    stops a measurement on an otherwise working bench."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "-i", str(gpu), f"--query-gpu={','.join(fields)}",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return {}
        vals = [v.strip() for v in out.stdout.strip().split(",")]
        return dict(zip(fields, vals))
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gpu", type=int, default=0)
    ap.add_argument("--size-mib", type=int, default=0,
                    help="per-buffer size; default = fit to free memory")
    ap.add_argument("--iters", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        import torch
    except ImportError:
        sys.exit("needs PyTorch with CUDA. Try an inference venv, e.g.\n"
                 "  ~/vllm-env/bin/python hx-bandwidth.py")
    if not torch.cuda.is_available():
        sys.exit("torch reports no CUDA device")

    torch.cuda.set_device(args.gpu)
    p = torch.cuda.get_device_properties(args.gpu)

    # Derived peak. Both operands come off the device, so this figure travels
    # with the card rather than with a spec sheet someone typed in.
    peak = p.memory_clock_rate * 1e3 * 2 * p.memory_bus_width / 8 / BYTES_PER_GB

    free_b, total_b = torch.cuda.mem_get_info(args.gpu)
    if args.size_mib:
        size_mib = args.size_mib
    else:
        # Three buffers plus headroom. Leave the tenant, if any, its slack:
        # OOM-ing a resident inference server to measure bandwidth is not a
        # trade worth making.
        size_mib = int(free_b / (1 << 20) * 0.70 / 3)
        size_mib = max(256, min(size_mib, 4096))

    n = size_mib * (1 << 20) // 4                    # float32 elements
    nbytes = n * 4

    # The working set must dwarf L2 or the kernel measures cache, not HBM.
    if nbytes < 8 * p.L2_cache_size:
        print(f"WARNING: buffer {nbytes/1e6:.0f} MB is small against "
              f"{p.L2_cache_size/1e6:.0f} MB of L2 — figures may read high",
              file=sys.stderr)

    pre = smi(["memory.used", "utilization.gpu", "clocks.mem", "clocks.max.mem",
               "power.draw", "temperature.gpu"], args.gpu)

    if not args.json:
        print(f"card        : {p.name}  ({p.multi_processor_count} SMs, "
              f"{p.total_memory/1e9:.1f} GB, {p.L2_cache_size/1e6:.0f} MB L2)")
        print(f"memory      : {p.memory_bus_width}-bit HBM2e @ "
              f"{p.memory_clock_rate/1e6:.3f} GHz")
        print(f"derived peak: {peak:.0f} GB/s  "
              f"(= {p.memory_bus_width} bit x {p.memory_clock_rate/1e6:.3f} GHz x 2 DDR)")
        used = pre.get("memory.used")
        if used and int(used) > 512:
            print(f"NOTE        : {used} MiB already resident — another process "
                  f"is sharing this memory system")
        print(f"working set : 3 x {size_mib} MiB, {args.iters} iterations\n")

    a = torch.empty(n, dtype=torch.float32, device="cuda")
    b = torch.empty(n, dtype=torch.float32, device="cuda")
    c = torch.empty(n, dtype=torch.float32, device="cuda")
    b.fill_(1.0)
    c.fill_(2.0)

    # Each kernel is paired with the bytes it is REQUIRED to move. Getting this
    # accounting wrong is the classic way to publish a bandwidth number that is
    # simply a factor of 2 or 3 out.
    tests = [
        ("read   (sum)",   lambda: torch.sum(b),                        nbytes),
        ("write  (fill)",  lambda: a.fill_(3.0),                        nbytes),
        ("copy   (a=b)",   lambda: a.copy_(b),                      2 * nbytes),
        ("scale  (a=s*b)", lambda: torch.mul(b, 3.0, out=a),         2 * nbytes),
        ("add    (a=b+c)", lambda: torch.add(b, c, out=a),           3 * nbytes),
        ("triad  (a=b+sc)", lambda: torch.add(b, c, alpha=3.0, out=a), 3 * nbytes),
    ]

    results = {}
    start, end = torch.cuda.Event(True), torch.cuda.Event(True)
    for name, fn, moved in tests:
        for _ in range(5):                                   # warm up / clock ramp
            fn()
        torch.cuda.synchronize()
        best = 0.0
        times = []
        for _ in range(args.iters):
            start.record()
            fn()
            end.record()
            torch.cuda.synchronize()
            ms = start.elapsed_time(end)
            times.append(ms)
            best = max(best, moved / (ms * 1e-3) / BYTES_PER_GB)
        times.sort()
        med = moved / (times[len(times) // 2] * 1e-3) / BYTES_PER_GB
        results[name.split()[0]] = {"best_gbs": best, "median_gbs": med,
                                    "pct_peak": 100 * best / peak}
        if not args.json:
            print(f"  {name:<16} {best:7.1f} GB/s best   {med:7.1f} median   "
                  f"{100*best/peak:5.1f}% of peak")

    post = smi(["clocks.mem", "power.draw", "temperature.gpu",
                "temperature.memory"], args.gpu)
    headline = max(v["best_gbs"] for v in results.values())

    if args.json:
        print(json.dumps({
            "card": p.name, "sms": p.multi_processor_count,
            "bus_width_bit": p.memory_bus_width,
            "memory_clock_ghz": p.memory_clock_rate / 1e6,
            "derived_peak_gbs": peak, "headline_gbs": headline,
            "pct_of_peak": 100 * headline / peak,
            "buffer_mib": size_mib, "iters": args.iters,
            "smi_before": pre, "smi_after": post,
        }, indent=2))
    else:
        print(f"\nheadline    : {headline:.0f} GB/s "
              f"({100*headline/peak:.0f}% of derived peak {peak:.0f} GB/s)")
        cm, cmax = post.get("clocks.mem"), pre.get("clocks.max.mem")
        if cm and cmax and cmax not in ("[N/A]", "N/A"):
            flag = "" if int(cm) >= int(cmax) * 0.95 else "   <-- DOWNCLOCKED"
            print(f"mem clock   : {cm} / {cmax} MHz under load{flag}")
        if post.get("power.draw"):
            print(f"under load  : {post['power.draw']} W, "
                  f"core {post.get('temperature.gpu','?')} C, "
                  f"HBM {post.get('temperature.memory','?')} C")
        if 100 * headline / peak < 70:
            print("\nBelow 70% of peak. Check clocks.mem against clocks.max.mem "
                  "and whether another process is using this GPU.")


if __name__ == "__main__":
    main()
