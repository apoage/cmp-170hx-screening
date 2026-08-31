#!/usr/bin/env python3
"""CMP 170HX bench — console status display.

Runs full-screen on tty1 after autologin, so the bench shows what it is doing
without anyone needing to log in or open a browser. Plain ANSI rather than
curses: it has to render correctly on a bare Linux console, over serial, and
through `ssh -t` alike.

Multi-card: every GPU the driver reports gets its own block, and a screen in
progress is attributed to the specific card it is testing (matched through the
`--gpu N` of the running hx-vram-screen.sh child), so a parallel batch shows two
independent progress bars rather than one ambiguous one.

Read-only. It never touches the GPU or the results, so leaving it running for
weeks cannot affect a screen in progress.

Quit with q or Ctrl-C.
"""
import os
import re
import shutil
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path

BASE = Path(os.environ.get("HX_BASE", str(Path.home() / "vramscreen")))
RESULTS = BASE / "results"
PORT = os.environ.get("HX_PORT", "8097")
RUNDIR = re.compile(r"^(?P<sn>[A-Za-z0-9._-]+)_(?P<stamp>\d{8}-\d{6})$")
TICK = 2.0
INTERVAL = 2.0          # telemetry sample period, seconds (matches the screen)
FRESH = 90.0            # a telemetry file older than this is not the live run
ANSI = re.compile(r"\033\[[0-9;]*m")

# 256-colour ANSI; safe on the Linux console.
R = "\033[0m"
DIM = "\033[2m"
B = "\033[1m"
OK = "\033[38;5;42m"
WARN = "\033[38;5;214m"
BAD = "\033[38;5;203m"
CORE = "\033[38;5;209m"
HBM = "\033[38;5;75m"
MUTE = "\033[38;5;245m"


def sh(cmd, timeout=4):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception:
        return ""


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def vis(s):
    """Printable width, ignoring the colour escapes."""
    return len(ANSI.sub("", s))


def short_bus(busid):
    """00000000:05:00.0 -> 05:00 — the form the reports and the tooling use."""
    if not busid:
        return "—"
    p = busid.split(":")
    if len(p) >= 2:
        return (p[-2] + ":" + p[-1]).split(".")[0]
    return busid


def clean_serial(s):
    s = (s or "").strip()
    if not s or "N/A" in s.upper() or "NOT SUPPORTED" in s.upper():
        return ""
    return s


# ------------------------------------------------------------------ hardware
def gpus():
    """Every GPU nvidia-smi can see, in index order. [] if the driver is gone."""
    out = sh(["nvidia-smi",
              "--query-gpu=index,name,serial,pci.bus_id,temperature.gpu,"
              "temperature.memory,power.draw,memory.used,memory.total,"
              "utilization.gpu",
              "--format=csv,noheader,nounits"])
    res = []
    for ln in out.splitlines():
        p = [x.strip() for x in ln.split(",")]
        if len(p) < 10:
            continue
        try:
            idx = int(p[0])
        except ValueError:
            continue
        res.append({"index": idx, "name": p[1], "sn": clean_serial(p[2]),
                    "bus": short_bus(p[3]), "core": num(p[4]), "mem": num(p[5]),
                    "pw": num(p[6]), "used": num(p[7]), "total": num(p[8]),
                    "util": num(p[9]), "run": None})
    res.sort(key=lambda g: g["index"])
    return res


# ------------------------------------------------------------------- screens
def running_screens():
    """{gpu_index: {"pid": str, "minutes": int}} for live per-card screens.

    The bracketed pattern is deliberate: a plain `pgrep -f hx-vram-screen`
    matches the shell that invoked it, which has repeatedly made an idle bench
    look busy. `--all`/`--parallel` launchers are skipped — they are the parent,
    not a card under test."""
    out = sh(["pgrep", "-af", "[h]x-vram-screen"])
    res = {}
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) < 2:
            continue
        pid, args = parts[0], parts[1:]
        i = 0
        while i < len(args) and not args[i].endswith("hx-vram-screen.sh"):
            i += 1
        if i >= len(args):
            continue                       # watcher/wrapper, not the script
        rest = args[i + 1:]
        gpu, mins, parent = None, None, False
        j = 0
        while j < len(rest):
            t = rest[j]
            if t == "--gpu" and j + 1 < len(rest):
                gpu = rest[j + 1]
                j += 2
                continue
            if t.startswith("--gpu="):
                gpu = t[len("--gpu="):]
                j += 1
                continue
            if t in ("--all", "--parallel", "--rollup"):
                parent = True
            elif not t.startswith("-") and mins is None and t.isdigit():
                mins = int(t)
            j += 1
        if gpu is None:
            if parent:
                continue                   # the batch launcher itself
            gpu = os.environ.get("GPU", "0")   # bare `hx-vram-screen.sh 15`
        try:
            gi = int(gpu)
        except ValueError:
            continue
        res[gi] = {"pid": pid, "minutes": mins or 12}
    return res


def proc_elapsed(pids):
    if not pids:
        return {}
    out = sh(["ps", "-o", "pid=,etimes=", "-p", ",".join(pids)])
    res = {}
    for ln in out.splitlines():
        p = ln.split()
        if len(p) >= 2:
            try:
                res[p[0]] = int(p[1])
            except ValueError:
                pass
    return res


def run_dirs():
    if not RESULTS.is_dir():
        return []
    try:
        ds = [d.name for d in RESULTS.iterdir()
              if d.is_dir() and RUNDIR.match(d.name)]
    except OSError:
        return []
    ds.sort(reverse=True)                  # stamped names sort chronologically
    return ds


def latest_dir():
    ds = run_dirs()
    return ds[0] if ds else None


def find_run_dir(g, dirs):
    """Newest results dir belonging to this physical card.

    Serial first (that is what the run is named after), then the PCI address
    recorded in 01-identity.csv — cards with a blanked InfoROM are filed under
    their UUID, so serial matching alone would lose them."""
    sn = g["sn"]
    if sn:
        for d in dirs:
            m = RUNDIR.match(d)
            s = m.group("sn")
            if s == sn or s.startswith(sn + "-"):
                return d
    for d in dirs[:12]:
        f = RESULTS / d / "01-identity.csv"
        try:
            if g["bus"] and g["bus"] in f.read_text(errors="replace"):
                return d
        except OSError:
            continue
    return None


def count_lines(p):
    try:
        with p.open("rb") as f:
            return sum(chunk.count(b"\n")
                       for chunk in iter(lambda: f.read(65536), b""))
    except OSError:
        return 0


def attach_runs(gs):
    """Fill g["run"] for every card a screen is currently testing."""
    procs = running_screens()
    if not procs:
        return
    et = proc_elapsed([v["pid"] for v in procs.values()])
    dirs = run_dirs()
    now = time.time()
    for g in gs:
        pr = procs.get(g["index"])
        if not pr:
            continue
        d = find_run_dir(g, dirs)
        elapsed, samples = None, 0
        if d:
            csv = RESULTS / d / "03-telemetry.csv"
            try:
                if now - csv.stat().st_mtime < FRESH:
                    samples = count_lines(csv)
                    elapsed = samples * INTERVAL / 60.0
                else:
                    d = None               # a previous run of the same card
            except OSError:
                d = None
        if elapsed is None:                # first seconds: sampler not up yet
            secs = et.get(pr["pid"])
            elapsed = (secs / 60.0) if secs is not None else 0.0
        g["run"] = {"minutes": pr["minutes"], "elapsed": elapsed,
                    "dir": d, "samples": samples}


# -------------------------------------------------------------------- recent
def recent(n=6):
    rows = []
    for d in run_dirs()[:n]:
        m = RUNDIR.match(d)
        md = ""
        rp = RESULTS / d / "{}.md".format(m.group("sn"))
        try:
            if rp.exists():
                md = rp.read_text(errors="replace")
        except OSError:
            md = ""

        def f(pat):
            g = re.search(pat, md, re.M)
            return g.group(1).strip() if g else "—"

        rows.append({
            "sn": m.group("sn"),
            "when": "{} {}".format(
                m.group("stamp")[4:6] + "-" + m.group("stamp")[6:8],
                m.group("stamp")[9:11] + ":" + m.group("stamp")[11:13]),
            "verdict": f(r"^\*\*Verdict:\*\*\s*\*\*(.+?)\*\*"),
            "prov": f(r"^## \d+\. Provenance — \*\*(.+?)\*\*"),
        })
    return rows


def colour_for(text):
    t = (text or "").upper()
    if t.startswith("FAIL") or "BAD" in t:
        return BAD
    if "REPASTE" in t or "REPAD" in t or "KERNEL" in t or "AS-PULLED" in t:
        return WARN
    if t in ("—", ""):
        return MUTE
    return OK


def bar(val, lo, hi, width, colour):
    """Horizontal gauge. Values outside [lo,hi] clamp rather than overflow."""
    width = max(1, width)
    if val is None:
        return MUTE + "·" * width + R
    frac = max(0.0, min(1.0, (val - lo) / float(hi - lo)))
    fill = int(round(frac * width))
    return colour + "█" * fill + DIM + "░" * (width - fill) + R


def temp_txt(v):
    return "{:.0f}°C".format(v) if v is not None else "—"


def vram_txt(g):
    if g["used"] is None or g["total"] is None:
        return "—"
    return "{:.1f} / {:.0f} GiB".format(g["used"] / 1024.0, g["total"] / 1024.0)


# ------------------------------------------------------------------ blocks
def status_txt(g):
    run = g.get("run")
    if not run:
        return MUTE + "idle" + R, "idle"
    tot = run["minutes"] or 15
    frac = max(0.0, min(1.0, run["elapsed"] / float(tot))) if tot else 0.0
    plain = "{:.1f} / {} min  {:3.0f}%".format(run["elapsed"], tot, frac * 100)
    return OK + B + "● " + R + OK + plain + R, plain


def block_full(g, w, blank):
    """Three lines per card: identity, temperatures, load + progress."""
    out = []
    sn = g["sn"] or "no serial"
    left = "  {}GPU {}{}  {}{}{}   {}s/n {}  ·  {}{}".format(
        B, g["index"], R, B, g["name"], R, MUTE, sn, g["bus"], R)
    st, _ = status_txt(g)
    gap = max(1, w - vis(left) - vis(st) - 2)
    out.append(left + " " * gap + st)

    barw = max(6, (w - 46) // 2)
    hbm_val = temp_txt(g["mem"]) if g["mem"] is not None else "no sensor"
    out.append("   {}core{} {:>6} {}   {}HBM{} {:>9} {}".format(
        CORE, R, temp_txt(g["core"]), bar(g["core"], 25, 95, barw, CORE),
        HBM, R, hbm_val,
        bar(g["mem"], 35, 100, barw, HBM) if g["mem"] is not None
        else MUTE + "·" * barw + R))

    pw = "{:.0f} W".format(g["pw"]) if g["pw"] is not None else "—"
    util = "{:.0f}%".format(g["util"]) if g["util"] is not None else "—"
    line3 = "   {}vram{} {:<16} {}power{} {:<7} {}load{} {:<5}".format(
        MUTE, R, vram_txt(g), MUTE, R, pw, MUTE, R, util)
    run = g.get("run")
    if run:
        tot = run["minutes"] or 15
        frac = max(0.0, min(1.0, run["elapsed"] / float(tot))) if tot else 0.0
        pw_ = max(6, w - vis(line3) - 8)
        fill = int(frac * pw_)
        line3 += "  " + OK + "█" * fill + DIM + "░" * (pw_ - fill) + R
    out.append(line3)
    if blank:
        out.append("")
    return out


def compact_barw(w):
    return max(4, (w - 60) // 2)


def compact_header(w):
    return "{}  # {:<13} {:>5} {} {:>5} {} {:>6} {:>4}  screen{}".format(
        MUTE, "card", "core", " " * compact_barw(w), "HBM",
        " " * compact_barw(w), "power", "load", R)


def block_compact(g, w):
    """One line per card, for benches with more GPUs than the console has rows."""
    barw = compact_barw(w)
    run = g.get("run")
    tail = ""
    if run:
        tot = run["minutes"] or 15
        tail = OK + "{:.1f}/{}m".format(run["elapsed"], tot) + R
    else:
        tail = MUTE + "idle" + R
    line = "  {}{}{} {:<13} {}{:>5}{} {} {}{:>5}{} {} {:>6} {:>4}  ".format(
        B, g["index"], R, g["name"].replace("NVIDIA ", "")[:13],
        CORE, temp_txt(g["core"]), R, bar(g["core"], 25, 95, barw, CORE),
        HBM, temp_txt(g["mem"]) if g["mem"] is not None else "  —", R,
        bar(g["mem"], 35, 100, barw, HBM),
        "{:.0f}W".format(g["pw"]) if g["pw"] is not None else "—",
        "{:.0f}%".format(g["util"]) if g["util"] is not None else "—")
    return [line + tail]


# ------------------------------------------------------------------ render
def render():
    size = shutil.get_terminal_size((100, 30))
    w = max(60, min(size.columns, 110))
    rows_avail = max(12, size.lines - 1)
    gs = gpus()
    attach_runs(gs)
    host = os.uname().nodename
    out = []
    a = out.append

    busy = sum(1 for g in gs if g.get("run"))
    head = "{}  CMP 170HX screening bench{}{}   {}{}".format(B, R, MUTE, host, R)
    tag = "{}{} GPU{} · {}{}".format(
        MUTE, len(gs), "s" if len(gs) != 1 else "",
        "{} screening".format(busy) if busy else "idle", R)
    gap = max(1, w - vis(head) - vis(tag) - 2)
    a(head + " " * gap + tag)
    a("{}  {}{}".format(MUTE, "─" * (w - 4), R))

    rec = recent()
    # 2 header + 1 rule + 1 blank + RECENT header + rows + blank + rule + footer
    overhead = 2 + 1 + 1 + 1 + 1 + 2
    n_rec = min(len(rec), 6)
    budget = rows_avail - overhead - n_rec
    if gs:
        if budget >= 4 * len(gs):
            mode = "full"
        elif budget >= 3 * len(gs):
            mode = "tight"
        else:
            mode = "compact"
            n_rec = max(0, min(n_rec, rows_avail - overhead - len(gs) - 1))
        if mode == "compact":
            a(compact_header(w))
            for g in gs:
                out.extend(block_compact(g, w))
        else:
            for i, g in enumerate(gs):
                out.extend(block_full(g, w, blank=(mode == "full"
                                                   and i < len(gs) - 1)))
    else:
        a("  {}No GPU visible to the driver.{}".format(BAD, R))
        a("  {}Check the card is seated and nvidia-smi works.{}".format(MUTE, R))
        a("")

    a("{}  {}{}".format(MUTE, "─" * (w - 4), R))

    if busy:
        a("  {}Testing VRAM at operating temperature — "
          "do not use the GPUs or power off.{}".format(MUTE, R))
    else:
        a("  {}Idle.  Start one:  {}hx 15{}   "
          "{}(or --parallel 15 for every card at once){}"
          .format(MUTE, B, R, MUTE, R))

    if n_rec:
        a("")
        a("  {}RECENT{}".format(MUTE, R))
        for r in rec[:n_rec]:
            c = colour_for(r["verdict"])
            p = colour_for(r["prov"])
            a("   {}  {:<16}  {}{:<24}{} {}{}{}".format(
                r["when"], r["sn"][:16], c, r["verdict"][:24], R,
                p, r["prov"][:11], R))

    a("{}  {}{}".format(MUTE, "─" * (w - 4), R))
    ip = (sh(["hostname", "-I"]).split() or ["?"])[0]
    a("{}  reports: http://{}:{}    q to quit{}".format(MUTE, ip, PORT, R))
    return "\n".join(out)


def main():
    sys.stdout.write("\033[?25l")            # hide cursor
    fd = sys.stdin.fileno()
    raw = None
    try:
        raw = termios.tcgetattr(fd)
        tty.setcbreak(fd)
    except Exception:
        pass                                  # not a tty (piped) — just redraw
    try:
        while True:
            sys.stdout.write("\033[H\033[2J" + render() + "\n")
            sys.stdout.flush()
            if raw:
                import select
                if select.select([sys.stdin], [], [], TICK)[0]:
                    if sys.stdin.read(1).lower() == "q":
                        break
            else:
                time.sleep(TICK)
    except KeyboardInterrupt:
        pass
    finally:
        if raw:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, raw)
            except Exception:
                pass
        sys.stdout.write("\033[?25h\033[2J\033[H")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
