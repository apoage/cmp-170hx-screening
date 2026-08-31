#!/usr/bin/env python3
"""CMP 170HX thermal-curve analysis -> repaste/repad triage + markdown report block.

Input CSV (nvidia-smi -l): timestamp, temp.gpu, temp.memory, power.draw, clocks.sm, throttle_hex

WHY THESE METRICS
  memtest_vulkan takes the 170HX to sustained full load within ~2 s and holds it
  there for the whole run (measured: 39 W -> 252 W on a small buffer, ~200-220 W
  across the full 63 GiB heap). Every card runs the SAME workload and therefore
  dissipates near-identical power, so the temperature it reaches is
  a direct read-out of how well heat leaves the die -- i.e. of the paste, the
  pads and the heatsink mount. That makes deg-C-per-watt comparable card-to-card
  across a pile, which absolute temperatures are not.

  HBM sits on its own sensor (temperature.memory) and on this card idles ~16 C
  ABOVE the core. Memory running hot relative to core points at the pads over
  the HBM stacks rather than the die paste -- a different repair.

REPASTE SIGNALS (in order of confidence)
  1. Thermal throttle asserted (mask 0x20/0x40)      -> urgent, card is losing perf now
  2. Still climbing at end of run (no plateau)       -> heat is not getting out
  3. High plateau at fixed 250 W                     -> poor interface
  4. Wide memory-over-core gap under load            -> HBM pads, not die paste
"""
import sys, statistics as st

THROTTLE_BITS = {
    0x0004: "SW power cap (expected under sustained load)",
    0x0008: "HW slowdown",
    0x0020: "SW THERMAL slowdown",
    0x0040: "HW THERMAL slowdown",
    0x0080: "HW power brake",
}

def load(path):
    """Returns (rows, has_hbm). Consumer cards report temperature.memory as N/A;
    that must degrade to a core-only analysis, not void every sample. Losing the
    whole thermal report because one column is absent is worse than saying so."""
    rows, na = [], 0
    for line in open(path):
        p = [x.strip() for x in line.split(",")]
        if len(p) < 6:
            continue
        try:
            core = float(p[1])
            try:
                mem = float(p[2])
            except ValueError:
                mem, na = core, na + 1     # mirror core; flagged in the report
            rows.append({
                "t": p[0], "core": core, "mem": mem,
                "pw": float(p[3]), "sm": float(p[4]),
                "thr": int(p[5], 16),
            })
        except ValueError:
            continue                        # header row
    return rows, (na == 0 and bool(rows))

def sparkline(vals, width=60):
    """ASCII curve — renders in a terminal, an email and a PDF alike."""
    blocks = "▁▂▃▄▅▆▇█"
    if not vals:
        return ""
    step = max(1, len(vals) // width)
    s = vals[::step][:width]
    lo, hi = min(s), max(s)
    if hi - lo < 0.5:
        return blocks[0] * len(s)
    return "".join(blocks[min(7, int((v - lo) / (hi - lo) * 7.99))] for v in s)

def slope_per_min(vals, secs_per_sample):
    """Least-squares slope, deg C per minute."""
    n = len(vals)
    if n < 4:
        return 0.0
    xs = [i * secs_per_sample / 60.0 for i in range(n)]
    mx, my = st.mean(xs), st.mean(vals)
    den = sum((x - mx) ** 2 for x in xs)
    return 0.0 if den == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, vals)) / den

def main():
    path = sys.argv[1]
    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
    rows, has_hbm = load(path)
    if len(rows) < 10:
        print("THERMAL: insufficient samples"); return 3

    # Isolate the LOADED window. The sampler starts before the test and keeps
    # running after it, so the head is pre-load idle and the tail is cool-down.
    # Including either wrecks both the plateau and the slope (measured: the
    # cool-down tail alone turned a flat curve into -6.9 C/min "falling").
    loaded = [i for i, r in enumerate(rows) if r["pw"] > 100]
    if len(loaded) < 8:
        print("THERMAL: card never reached load — check that the test actually ran"); return 3
    lo, hi = loaded[0], loaded[-1]
    pre = rows[:lo]
    run = rows[lo:hi + 1]

    core = [r["core"] for r in run]
    mem  = [r["mem"]  for r in run]
    avg_pw = st.mean([r["pw"] for r in run])

    # Baseline = idle samples before load; fall back to the coolest of the run.
    if len(pre) >= 2:
        base_core = st.mean([r["core"] for r in pre])
        base_mem  = st.mean([r["mem"]  for r in pre])
    else:
        base_core, base_mem = st.mean(sorted(core)[:3]), st.mean(sorted(mem)[:3])

    # Plateau = last 25% of the LOADED window.
    tail = max(4, len(run) // 4)
    plat_core, plat_mem = st.mean(core[-tail:]), st.mean(mem[-tail:])

    # Has it stopped climbing? Slope over the final third.
    third = max(4, len(rows) // 3)
    s_core, s_mem = slope_per_min(core[-third:], interval), slope_per_min(mem[-third:], interval)

    rise_core, rise_mem = plat_core - base_core, plat_mem - base_mem
    cw_core = rise_core / avg_pw
    cw_mem  = rise_mem  / avg_pw
    gap_load = plat_mem - plat_core
    gap_idle = base_mem - base_core

    thr_seen = 0
    for r in run:
        thr_seen |= r["thr"]
    thermal_throttled = bool(thr_seen & (0x0020 | 0x0040))

    # ---- triage -------------------------------------------------------
    flags, verdict = [], "OK"
    if thermal_throttled:
        flags.append("THERMAL THROTTLE asserted during the run"); verdict = "REPASTE — URGENT"
    # A short run has not had time to plateau, so a positive slope means nothing.
    # Measured on a known-good card: still +3.6 C/min at 84 s, flat by ~6 min.
    run_min = len(run) * interval / 60.0
    if run_min < 5.0:
        flags.append(f"run only {run_min:.1f} min under load — too short to judge plateau; "
                     f"slope not used for the verdict (want >= 8 min)")
    elif s_core > 2.0 or s_mem > 2.0:
        flags.append(f"still climbing at end of run (core {s_core:+.1f} C/min, mem {s_mem:+.1f} C/min) — no plateau reached")
        if verdict == "OK": verdict = "REPASTE CANDIDATE"
    if has_hbm and plat_mem >= 90:
        flags.append(f"HBM plateau {plat_mem:.0f} C — at/over the safe band")
        if verdict == "OK": verdict = "REPASTE CANDIDATE"
    if plat_core >= 80:
        flags.append(f"core plateau {plat_core:.0f} C at only {avg_pw:.0f} W")
        if verdict == "OK": verdict = "REPASTE CANDIDATE"
    if has_hbm and gap_load > 25:
        flags.append(f"memory runs {gap_load:.0f} C over core under load — suspect HBM PADS rather than die paste")
        if verdict == "OK": verdict = "REPAD CANDIDATE"

    # ---- markdown -----------------------------------------------------
    print("### Thermal curve\n")
    print(f"Run length {len(run)*interval/60:.1f} min under load, sampled every {interval:g} s, "
          f"card held at **{avg_pw:.0f} W** throughout. Every card runs the identical "
          f"workload, so the temperature reached is a direct measure of heat transfer.\n")
    print("```")
    print(f"core {min(core):.0f}-{max(core):.0f}C  {sparkline(core)}")
    if has_hbm:
        print(f"HBM  {min(mem):.0f}-{max(mem):.0f}C  {sparkline(mem)}")
    print("```\n")
    hcol = (lambda v: v) if has_hbm else (lambda v: "no sensor")
    print("| Metric | Core | HBM | Reading |")
    print("|---|---|---|---|")
    print(f"| Idle baseline | {base_core:.0f} C | {hcol(f'{base_mem:.0f} C')} | {'gap ' + format(gap_idle, '.0f') + ' C' if has_hbm else ''} |")
    print(f"| Plateau (last 25%) | {plat_core:.0f} C | {hcol(f'{plat_mem:.0f} C')} | {'gap ' + format(gap_load, '.0f') + ' C' if has_hbm else ''} |")
    print(f"| Rise over idle | +{rise_core:.0f} C | {hcol(f'+{rise_mem:.0f} C')} | at {avg_pw:.0f} W |")
    print(f"| Thermal resistance | {cw_core:.3f} C/W | {hcol(f'{cw_mem:.3f} C/W')} | lower = better contact |")
    print(f"| End-of-run slope | {s_core:+.1f} C/min | {hcol(f'{s_mem:+.1f} C/min')} | >+2 = not plateaued |")
    print(f"| Peak | {max(core):.0f} C | {hcol(f'{max(mem):.0f} C')} | |")
    active = [d for b, d in THROTTLE_BITS.items() if thr_seen & b] or ["none"]
    print(f"| Throttle reasons | {', '.join(active)} | | |")
    print()
    if not has_hbm:
        print("_This card exposes no memory-temperature sensor, so only core "
              "temperature was measured. HBM figures are unavailable, not zero._\n")
    print(f"**Thermal verdict: {verdict}**\n")
    if flags:
        for f in flags:
            print(f"- {f}")
    else:
        print("- Plateaued cleanly, no thermal throttling, temperatures within band.")
    print()
    print("_Comparison note: this card is passively cooled (no onboard fan sensor), so "
          "absolute temperatures depend on bench airflow. Screen the whole batch on the "
          "same bench and compare C/W across cards — the batch median is the reference, "
          "and outliers are the repaste candidates._")

    # machine-readable line for the fleet roll-up
    print(f"\n<!--FLEET,{plat_core:.1f},{plat_mem:.1f},{cw_core:.4f},{cw_mem:.4f},"
          f"{s_core:.2f},{s_mem:.2f},{avg_pw:.0f},{int(thermal_throttled)},{verdict}-->")
    return 0

if __name__ == "__main__":
    sys.exit(main())
