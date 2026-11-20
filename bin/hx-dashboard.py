#!/usr/bin/env python3
"""CMP 170HX screening dashboard — browse runs, watch the live one, export a
customer-ready report for any card.

Deliberately stdlib-only: this runs unattended on the bench box from boot, so it
must not depend on pip, a venv, or network access. It only ever READS from
~/vramscreen/results, so it can never disturb a screening run in progress.

Routes
  /                                 index: every card screened, newest first
  /run/<dir>                        detail: full report + thermal chart
  /run/<dir>/customer.html          self-contained page to send the buyer
  /run/<dir>/report.md              raw markdown
  /run/<dir>/raw/<file>             any raw log
  /api/status                       JSON, for scripting
  /api/live                         JSON, one entry per GPU + telemetry history
"""
import html, json, os, re, socketserver, subprocess, time, urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path

BASE     = Path(os.environ.get("HX_BASE", str(Path.home() / "vramscreen")))
RESULTS  = BASE / "results"
PORT     = int(os.environ.get("HX_PORT", "8097"))
RUNDIR   = re.compile(r"^(?P<sn>[A-Za-z0-9._-]+)_(?P<stamp>\d{8}-\d{6})$")
INTERVAL = 2.0        # telemetry sample period, seconds (set by the screen)
FRESH    = 90.0       # telemetry older than this is not the run in progress


# --------------------------------------------------------------------- data
def parse_stamp(s):
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]} {s[9:11]}:{s[11:13]}"


def field(md, pat, default="—"):
    m = re.search(pat, md, re.M)
    return m.group(1).strip() if m else default


def list_runs():
    runs = []
    if not RESULTS.is_dir():
        return runs
    for d in RESULTS.iterdir():
        m = RUNDIR.match(d.name)
        if not m or not d.is_dir():
            continue
        rp = d / f"{m.group('sn')}.md"
        md = rp.read_text(errors="replace") if rp.exists() else ""
        runs.append({
            "dir": d.name,
            "sn": m.group("sn"),
            "stamp": m.group("stamp"),
            "when": parse_stamp(m.group("stamp")),
            "verdict": field(md, r"^\*\*Verdict:\*\*\s*\*\*(.+?)\*\*"),
            "provenance": field(md, r"^## \d+\. Provenance — \*\*(.+?)\*\*"),
            "vram": field(md, r"^## \d+\. VRAM integrity — \*\*(.+?)\*\*"),
            "thermal": field(md, r"^\*\*Thermal verdict:\s*(.+?)\*\*"),
            "plateau": field(md, r"^\| Plateau \(last 25%\) \| (.+?) \|"),
            "has_report": rp.exists(),
        })
    runs.sort(key=lambda r: r["stamp"], reverse=True)
    return runs


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def telemetry(d):
    """(core, hbm, watts) per sample. hbm is None on cards with no HBM sensor —
    those rows still carry a usable core temperature and must not be dropped."""
    f = RESULTS / d / "03-telemetry.csv"
    rows = []
    try:
        text = f.read_text(errors="replace")
    except OSError:
        return rows
    for line in text.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 6:
            continue                       # header, blank, or a partial line
        core, watts = num(p[1]), num(p[3])
        if core is None or watts is None:
            continue
        rows.append((core, num(p[2]), watts))
    return rows


def sh(cmd, timeout=4):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception:
        return ""


def live():
    """Is a screen running right now, and how far in?  (legacy /api/status)"""
    # Bracketed pattern: a plain `-f hx-vram-screen.sh` also matches the process
    # doing the asking, which reports an idle bench as busy.
    if not sh(["pgrep", "-f", "[h]x-vram-screen"]):
        return None
    runs = list_runs()
    if not runs:
        return {"dir": None, "samples": 0, "minutes": 0.0}
    rows = telemetry(runs[0]["dir"])
    cur = rows[-1] if rows else None
    return {"dir": runs[0]["dir"], "sn": runs[0]["sn"], "samples": len(rows),
            "minutes": round(len(rows) * INTERVAL / 60, 1),
            "core": cur[0] if cur else None, "hbm": cur[1] if cur else None,
            "watts": cur[2] if cur else None}


# ------------------------------------------------------------- live per-GPU
def short_bus(busid):
    """00000000:05:00.0 -> 05:00, the form the reports and the tooling use."""
    if not busid:
        return ""
    p = busid.split(":")
    if len(p) >= 2:
        return (p[-2] + ":" + p[-1]).split(".")[0]
    return busid


def clean_serial(s):
    s = (s or "").strip()
    if not s or "N/A" in s.upper() or "NOT SUPPORTED" in s.upper():
        return ""
    return s


def nv_gpus():
    """Every GPU the driver reports. Empty list if nvidia-smi is unavailable."""
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
        res.append({"index": idx, "name": p[1], "serial": clean_serial(p[2]),
                    "pci": p[3], "bus": short_bus(p[3]),
                    "core": num(p[4]), "hbm": num(p[5]), "power": num(p[6]),
                    "mem_used": num(p[7]), "mem_total": num(p[8]),
                    "util": num(p[9]),
                    "running": False, "elapsed_min": 0.0, "total_min": 0,
                    "pct": 0.0, "dir": None, "samples": 0, "history": [],
                    "history_src": ""})
    res.sort(key=lambda g: g["index"])
    return res


def running_screens():
    """{gpu_index: {"pid": str, "minutes": int}} for live per-card screens.

    Bracketed pattern so it cannot match its own invocation. `--all`/`--parallel`
    launchers are the parent of the real per-card children and are skipped."""
    res = {}
    for ln in sh(["pgrep", "-af", "[h]x-vram-screen"]).splitlines():
        parts = ln.split()
        if len(parts) < 2:
            continue
        pid, args = parts[0], parts[1:]
        i = 0
        while i < len(args) and not args[i].endswith("hx-vram-screen.sh"):
            i += 1
        if i >= len(args):
            continue                        # a watcher/wrapper, not the script
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
                continue
            gpu = "0"                       # bare `hx-vram-screen.sh 15`
        try:
            gi = int(gpu)
        except ValueError:
            continue
        res[gi] = {"pid": pid, "minutes": mins or 12}
    return res


def proc_elapsed(pids):
    res = {}
    if not pids:
        return res
    for ln in sh(["ps", "-o", "pid=,etimes=", "-p", ",".join(pids)]).splitlines():
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
    ds.sort(reverse=True)                   # stamped names sort chronologically
    return ds


def find_run_dir(g, dirs):
    """Newest results dir belonging to this physical card: serial first (runs are
    named after it), then the PCI address in 01-identity.csv — a card with a
    blanked InfoROM is filed under its UUID and serial matching would miss it."""
    sn = g.get("serial")
    if sn:
        for d in dirs:
            s = RUNDIR.match(d).group("sn")
            if s == sn or s.startswith(sn + "-"):
                return d
    if g.get("bus"):
        for d in dirs[:12]:
            try:
                if g["bus"] in (RESULTS / d / "01-identity.csv").read_text(
                        errors="replace"):
                    return d
            except OSError:
                continue
    return None


def downsample(rows, maxpts=220):
    """Thin the curve for the browser without losing its shape."""
    n = len(rows)
    if n <= maxpts:
        return list(range(n))
    step = float(n - 1) / (maxpts - 1)
    return sorted(set(int(round(i * step)) for i in range(maxpts)))


def curve(d):
    """The run's temperature curve, thinned for the browser: [minutes, core, hbm]."""
    rows = telemetry(d)
    return [[round(i * INTERVAL / 60.0, 3), rows[i][0], rows[i][1]]
            for i in downsample(rows)], len(rows)


def live_gpus():
    """Per-GPU snapshot plus a temperature curve: the run in progress on that
    card, or failing that its most recent completed screen, so an idle card
    still shows something meaningful instead of a flat line."""
    gs = nv_gpus()
    procs = running_screens()
    et = proc_elapsed([v["pid"] for v in procs.values()])
    dirs = run_dirs()
    now = time.time()
    for g in gs:
        pr = procs.get(g["index"])
        if not pr:
            d = find_run_dir(g, dirs)         # idle: show the last screen
            if d:
                try:
                    g["history"], g["samples"] = curve(d)
                    g["dir"] = d
                    g["history_src"] = "last screen · " + parse_stamp(
                        RUNDIR.match(d).group("stamp"))
                except OSError:
                    pass
            continue
        g["running"] = True
        g["total_min"] = pr["minutes"] or 15
        d = find_run_dir(g, dirs)
        rows, fresh = [], False
        if d:
            csv = RESULTS / d / "03-telemetry.csv"
            try:
                fresh = (now - csv.stat().st_mtime) < FRESH
            except OSError:
                fresh = False
            if fresh:
                rows = telemetry(d)
                g["dir"] = d
        if fresh:
            g["samples"] = len(rows)
            g["elapsed_min"] = round(len(rows) * INTERVAL / 60.0, 2)
            g["history"] = [[round(i * INTERVAL / 60.0, 3),
                             rows[i][0], rows[i][1]] for i in downsample(rows)]
            g["history_src"] = "this run"
        else:
            secs = et.get(pr["pid"])          # sampler not up yet, or stale file
            g["elapsed_min"] = round((secs or 0) / 60.0, 2)
        if g["total_min"]:
            g["pct"] = round(min(100.0, 100.0 * g["elapsed_min"]
                                 / g["total_min"]), 1)
    return gs


# ------------------------------------------------------------------ drawing
def chart(rows, w=880, h=300):
    """Inline SVG: HBM and core temperature over the run, power as a shaded band.

    Hand-drawn rather than pulled from a chart library so the page stays
    dependency-free and renders identically in a browser, a PDF and an email."""
    if len(rows) < 4:
        return '<p class="muted">No telemetry recorded for this run.</p>'
    pad_l, pad_r, pad_t, pad_b = 52, 52, 16, 34
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b
    core = [r[0] for r in rows]
    hbm = [r[1] for r in rows]
    pw = [r[2] for r in rows]
    hbmv = [v for v in hbm if v is not None]      # cards without an HBM sensor
    tmin = max(0, min(core + hbmv) - 5)
    tmax = max(core + hbmv) + 5
    if tmax - tmin < 10:
        tmax = tmin + 10
    pmax = max(max(pw), 1) * 1.15
    n = len(rows)

    def X(i):
        return pad_l + iw * i / (n - 1)

    def Y(t):
        return pad_t + ih * (1 - (t - tmin) / (tmax - tmin))

    def YP(p):
        return pad_t + ih * (1 - p / pmax)

    def path(vals, fn):
        """Gaps (None) break the line rather than dropping it to zero."""
        seg, started = [], False
        for i, v in enumerate(vals):
            if v is None:
                started = False
                continue
            seg.append(("L" if started else "M") + f"{X(i):.1f},{fn(v):.1f}")
            started = True
        return " ".join(seg)

    grid, labels = [], []
    step = 10 if tmax - tmin > 40 else 5
    t = int(tmin // step * step) + step
    while t < tmax:
        y = Y(t)
        grid.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + iw}" y2="{y:.1f}" class="grid"/>')
        labels.append(f'<text x="{pad_l - 8}" y="{y + 4:.1f}" class="ax" text-anchor="end">{t}°</text>')
        t += step
    mins = n * 2 / 60
    for frac in (0, .25, .5, .75, 1):
        x = pad_l + iw * frac
        labels.append(f'<text x="{x:.1f}" y="{h - 12}" class="ax" '
                      f'text-anchor="middle">{mins * frac:.0f}m</text>')
    labels.append(f'<text x="{pad_l + iw + 8}" y="{YP(pmax * 0.87) + 4:.0f}" '
                  f'class="ax pw">{pmax * 0.87:.0f}W</text>')
    tail = pw[len(pw) // 2:]
    avg_pw = sum(tail) / max(1, len(tail))

    return f'''<svg viewBox="0 0 {w} {h}" class="chart" role="img"
     aria-label="Core and memory temperature across the run">
  <defs><linearGradient id="pg" x1="0" x2="0" y1="0" y2="1">
    <stop offset="0%" stop-color="var(--pw)" stop-opacity=".22"/>
    <stop offset="100%" stop-color="var(--pw)" stop-opacity="0"/>
  </linearGradient></defs>
  {''.join(grid)}
  <path d="{path(pw, YP)} L{X(n - 1):.1f},{pad_t + ih} L{pad_l},{pad_t + ih} Z" fill="url(#pg)"/>
  <path d="{path(pw, YP)}" class="l-pw"/>
  <path d="{path(hbm, Y)}" class="l-hbm"/>
  <path d="{path(core, Y)}" class="l-core"/>
  {''.join(labels)}
</svg>
<div class="legend">
  <span><i class="k-hbm"></i>HBM memory &nbsp;<b>{(str(int(round(hbmv[-1]))) + "°C") if hbmv else "no sensor"}</b></span>
  <span><i class="k-core"></i>GPU core &nbsp;<b>{core[-1]:.0f}°C</b></span>
  <span><i class="k-pw"></i>power &nbsp;<b>{avg_pw:.0f} W</b></span>
</div>'''


# ------------------------------------------------------------------ markdown
def inline(s):
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    # whole-word underscores only: a bare _(.+?)_ rule turns fuse-word names
    # like fbp_defective 0x840 fbp_disable into italic garbage
    s = re.sub(r"(?<![\w])_([^_\n]+?)_(?![\w])", r"<em>\1</em>", s)
    return s


def md_to_html(md):
    """Small deliberate subset: headings, tables, fences, bold, lists."""
    out, i, lines = [], 0, md.splitlines()
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```"):
            block = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(html.escape(lines[i]))
                i += 1
            out.append("<pre>" + "\n".join(block) + "</pre>")
            i += 1
            continue
        if (ln.startswith("|") and i + 1 < len(lines)
                and set(lines[i + 1].replace("|", "").strip()) <= set("-: ")):
            head = [c.strip() for c in ln.strip("|").split("|")]
            i += 2
            body = []
            while i < len(lines) and lines[i].startswith("|"):
                body.append([c.strip() for c in lines[i].strip("|").split("|")])
                i += 1
            th = "".join(f"<th>{inline(c)}</th>" for c in head)
            tr = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>"
                         for r in body)
            out.append(f"<table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table>")
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", ln)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>")
            i += 1
            continue
        if ln.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append(f"<li>{inline(lines[i][2:])}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        if not ln.strip():
            i += 1
            continue
        para = []
        while i < len(lines) and lines[i].strip() and lines[i][0] not in "#|-`":
            para.append(lines[i])
            i += 1
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
        else:
            i += 1
    return "\n".join(out)


# ---------------------------------------------------------------------- css
CSS = """
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1a1a18;--muted:#6b6b66;--line:#e2e2dd;
      --ok:#1f7a4d;--warn:#a86b12;--bad:#b3261e;--core:#c2410c;--hbm:#1d4ed8;
      --pw:#6b6b66;--accent:#1a1a18}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
  --bg:#141413;--panel:#1c1c1a;--ink:#ecece8;--muted:#9a9a93;--line:#2e2e2b;
  --ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--core:#fb923c;--hbm:#60a5fa;
  --pw:#8a8a83;--accent:#ecece8}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
     font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:32px 20px 72px}
header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:6px}
h1{font-size:21px;margin:0;letter-spacing:-.01em}
h2{font-size:16px;margin:30px 0 10px;letter-spacing:-.01em}
h3{font-size:14px;margin:22px 0 8px;color:var(--muted);text-transform:uppercase;
   letter-spacing:.06em}
.muted{color:var(--muted)}
.sub{color:var(--muted);font-size:13px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
      padding:20px;margin:16px 0}
table{border-collapse:collapse;width:100%;font-size:14px;margin:10px 0;display:block;
      overflow-x:auto}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
   font-weight:600}
tbody tr:last-child td{border-bottom:0}
a{color:var(--accent)}
a.row{text-decoration:none;color:inherit}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:12px;
    overflow-x:auto;font-size:12.5px;line-height:1.5}
code{font-size:12.5px;background:var(--bg);padding:1px 5px;border-radius:4px}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:11.5px;
      font-weight:600;letter-spacing:.02em;border:1px solid currentColor}
.p-ok{color:var(--ok)} .p-warn{color:var(--warn)} .p-bad{color:var(--bad)}
.chart{width:100%;height:auto;display:block;margin:4px 0 2px}
.grid{stroke:var(--line);stroke-width:1}
.ax{fill:var(--muted);font-size:11px}
.ax.pw{fill:var(--pw)}
.l-core{fill:none;stroke:var(--core);stroke-width:2;stroke-linejoin:round}
.l-hbm{fill:none;stroke:var(--hbm);stroke-width:2;stroke-linejoin:round}
.l-pw{fill:none;stroke:var(--pw);stroke-width:1;stroke-dasharray:3 3;opacity:.75}
.legend{display:flex;gap:20px;flex-wrap:wrap;font-size:12.5px;color:var(--muted);
        margin-top:4px}
.legend i{display:inline-block;width:11px;height:3px;border-radius:2px;margin-right:6px;
          vertical-align:middle}
.k-core{background:var(--core)} .k-hbm{background:var(--hbm)} .k-pw{background:var(--pw)}
.live{border-color:var(--ok)}
.dot{display:inline-block;width:8px;height:8px;border-radius:99px;background:var(--ok);
     margin-right:7px;animation:p 1.6s ease-in-out infinite}
@keyframes p{0%,100%{opacity:1}50%{opacity:.35}}
.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
.btn{display:inline-block;padding:7px 14px;border:1px solid var(--line);border-radius:8px;
     background:var(--panel);text-decoration:none;color:var(--ink);font-size:13px;
     font-weight:500}
.btn:hover{border-color:var(--muted)}
.btn.primary{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:1px;
       background:var(--line);border:1px solid var(--line);border-radius:10px;
       overflow:hidden;margin:14px 0}
.stat{background:var(--panel);padding:12px 14px}
.stat b{display:block;font-size:19px;font-weight:600;letter-spacing:-.01em}
.stat span{font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.gwrap{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;
       margin-top:12px}
.gcard{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.gcard .gname{font-size:14px;font-weight:600;letter-spacing:-.01em;
              display:flex;align-items:center;gap:0}
.gcard .stats{margin:11px 0 0}
.gcard .stat b{font-size:16px}
.prog{height:6px;border-radius:99px;background:var(--line);overflow:hidden;margin:11px 0 5px}
.prog i{display:block;height:100%;background:var(--ok);transition:width .5s linear}
.mini{width:100%;height:auto;display:block;margin-top:10px}
.idle{opacity:.72}
@media print{body{background:#fff}.card{border:0;padding:0}.actions,.back{display:none}}
"""

# Vanilla, inline, no CDN: the bench has no outbound network. Polls /api/live and
# redraws one card per GPU. The temperature curve comes from the run's telemetry
# CSV while a screen is active (so it shows the WHOLE run, not just since the tab
# was opened); on an idle card it falls back to accumulating its own samples.
LIVE_JS = r"""
(function(){
  var host=document.getElementById('livecards'); if(!host) return;
  var title=document.getElementById('livetitle');
  var hist={}, t0={}, sig=null;
  function esc(s){var d=document.createElement('div');d.textContent=(s==null?'':s);return d.innerHTML;}
  function n(v,d){return (v===null||v===undefined||isNaN(v))?'—':Number(v).toFixed(d||0);}
  function st(v,l){return '<div class="stat"><b>'+v+'</b><span>'+l+'</span></div>';}
  function spark(pts){
    if(!pts||pts.length<3) return '<p class="muted" style="font-size:12.5px;margin:10px 0 0">no telemetry yet</p>';
    var W=320,H=104,pl=32,pr=6,ptop=8,pb=16,iw=W-pl-pr,ih=H-ptop-pb;
    var x0=pts[0][0],x1=pts[pts.length-1][0]; if(!(x1>x0)) x1=x0+1;
    var vals=[],i,k;
    for(i=0;i<pts.length;i++){for(k=1;k<3;k++){if(pts[i][k]!==null&&pts[i][k]!==undefined)vals.push(pts[i][k]);}}
    if(!vals.length) return '<p class="muted" style="font-size:12.5px;margin:10px 0 0">no telemetry yet</p>';
    var lo=Math.min.apply(null,vals)-3, hi=Math.max.apply(null,vals)+3;
    if(hi-lo<12){hi=lo+12;}
    function X(v){return pl+iw*(v-x0)/(x1-x0);}
    function Y(v){return ptop+ih*(1-(v-lo)/(hi-lo));}
    function path(k){var d='',on=false,j,v;
      for(j=0;j<pts.length;j++){v=pts[j][k];
        if(v===null||v===undefined){on=false;continue;}
        d+=(on?'L':'M')+X(pts[j][0]).toFixed(1)+','+Y(v).toFixed(1)+' ';on=true;}
      return d;}
    var step=(hi-lo)>45?20:10, t=Math.ceil(lo/step)*step, g='',lab='';
    for(;t<hi;t+=step){var y=Y(t);
      g+='<line class="grid" x1="'+pl+'" y1="'+y.toFixed(1)+'" x2="'+(pl+iw)+'" y2="'+y.toFixed(1)+'"/>';
      lab+='<text class="ax" x="'+(pl-5)+'" y="'+(y+3.5).toFixed(1)+'" text-anchor="end">'+t+'°</text>';}
    lab+='<text class="ax" x="'+pl+'" y="'+(H-3)+'">0m</text>';
    lab+='<text class="ax" x="'+(pl+iw)+'" y="'+(H-3)+'" text-anchor="end">'+x1.toFixed(x1<10?1:0)+'m</text>';
    var ph=path(2);
    return '<svg class="mini" viewBox="0 0 '+W+' '+H+'" role="img" aria-label="core and memory temperature over time">'
      +g+(ph?'<path class="l-hbm" d="'+ph+'"/>':'')+'<path class="l-core" d="'+path(1)+'"/>'+lab+'</svg>';
  }
  function vram(g){
    if(g.mem_used===null||g.mem_total===null) return '—';
    return (g.mem_used/1024).toFixed(1)+' / '+(g.mem_total/1024).toFixed(0)+' GiB';
  }
  function card(g){
    var h='<div class="gcard'+(g.running?'':' idle')+'">';
    h+='<div class="gname">'+(g.running?'<span class="dot"></span>':'')
      +'GPU '+g.index+' · '+esc(g.name)+'</div>';
    h+='<div class="sub">s/n '+esc(g.serial||'no serial')+' · slot '+esc(g.bus||'—')+'</div>';
    h+='<div class="stats">'+st(n(g.core)+'°C','core')
      +st(g.hbm===null?'no sensor':n(g.hbm)+'°C','HBM')
      +st(n(g.power)+' W','power')+st(vram(g),'vram')+st(n(g.util)+' %','load')
      +st(g.running?(n(g.elapsed_min,1)+' / '+g.total_min+' m'):'idle','screen')+'</div>';
    if(g.running){
      h+='<div class="prog"><i style="width:'+Math.max(0,Math.min(100,g.pct)).toFixed(1)+'%"></i></div>';
      h+='<div class="sub">'+n(g.pct)+'% · '+g.samples+' samples</div>';
    }else{
      h+='<div class="prog" style="visibility:hidden"><i style="width:0"></i></div>';
      h+='<div class="sub">idle — no screen running on this card</div>';
    }
    var a=hist[g.index], sv=spark(a);
    h+=sv;
    if(sv.indexOf('<svg')===0){
      h+='<div class="legend"><span><i class="k-core"></i>core</span>'
        +'<span><i class="k-hbm"></i>HBM</span>'
        +'<span style="margin-left:auto">'
        +esc((a&&a.srv&&g.history_src)?g.history_src:'since this page opened')
        +'</span></div>';
    }
    return h+'</div>';
  }
  function draw(d){
    var gs=(d&&d.gpus)||[], i, g, a;
    var s=gs.map(function(x){return x.index+':'+(x.running?1:0);}).join(',');
    if(sig!==null&&s!==sig){location.reload();return;}   // a run started/finished
    sig=s;
    for(i=0;i<gs.length;i++){
      g=gs[i];
      if(g.history&&g.history.length){ hist[g.index]=g.history; hist[g.index].srv=true; }
      else{
        a=hist[g.index];
        if(!a||a.srv){a=[];hist[g.index]=a;delete t0[g.index];}
        if(!(g.index in t0)) t0[g.index]=d.ts;
        a.push([(d.ts-t0[g.index])/60,g.core,g.hbm]);
        if(a.length>240)a.shift();
      }
    }
    var busy=gs.filter(function(x){return x.running;}).length;
    if(title) title.innerHTML=(busy?'<span class="dot"></span>Screening in progress'
      :'Live GPU status')+' <span class="sub" style="font-weight:400">· '
      +gs.length+' GPU'+(gs.length===1?'':'s')+(busy?' · '+busy+' under test':'')+'</span>';
    host.innerHTML=gs.length?gs.map(card).join('')
      :'<p class="muted">No GPU is visible to the driver.</p>';
  }
  function poll(){
    try{
      fetch('/api/live',{cache:'no-store'})
        .then(function(r){return r.json();}).then(draw)['catch'](function(){});
    }catch(e){}
  }
  var boot=document.getElementById('live0');
  if(boot){try{draw(JSON.parse(boot.textContent));}catch(e){}}
  poll(); setInterval(poll,3000);
})();
"""


def pill(v):
    t = (v or "").upper()
    if t.startswith("FAIL") or "BAD" in t:
        c = "p-bad"
    elif "REPASTE" in t or "REPAD" in t or "KERNEL" in t or "AS-PULLED" in t:
        c = "p-warn"
    else:
        c = "p-ok"
    return f'<span class="pill {c}">{html.escape(v or "—")}</span>'


def page(title, body, head_extra=""):
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>{head_extra}<style>{CSS}</style></head>
<body><div class="wrap">{body}</div></body></html>"""


# -------------------------------------------------------------------- pages
def live_panel(gs):
    """One live card per GPU. The first paint is served inline so the panel is
    populated before the first poll; the script then updates it in place."""
    if not gs:
        return ""
    busy = sum(1 for g in gs if g["running"])
    heading = ('<span class="dot"></span>Screening in progress' if busy
               else "Live GPU status")
    boot = json.dumps({"ts": time.time(), "gpus": gs}).replace("</", "<\\/")
    ns = "".join(
        '<tr><td>GPU {}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>'.format(
            g["index"], html.escape(g["serial"] or "no serial"),
            "—" if g["core"] is None else "{:.0f}°C".format(g["core"]),
            "no sensor" if g["hbm"] is None else "{:.0f}°C".format(g["hbm"]),
            "{} / {} min".format(g["elapsed_min"], g["total_min"])
            if g["running"] else "idle")
        for g in gs)
    return (
        '<div class="card{}">'.format(" live" if busy else "")
        + '<h2 id="livetitle">{} <span class="sub" style="font-weight:400">· {} GPU{}{}</span></h2>'
        .format(heading, len(gs), "" if len(gs) == 1 else "s",
                " · {} under test".format(busy) if busy else "")
        + '<div class="gwrap" id="livecards"></div>'
        + '<script type="application/json" id="live0">{}</script>'.format(boot)
        + '<noscript><table><thead><tr><th>GPU</th><th>Serial</th><th>Core</th>'
          '<th>HBM</th><th>Screen</th></tr></thead><tbody>{}</tbody></table>'
          '</noscript>'.format(ns)
        + '</div>')


def index_page():
    runs = list_runs()
    try:
        gs = live_gpus()
    except Exception:
        gs = []                       # a card that vanished must not 500 the page
    lv = any(g["running"] for g in gs)
    b = ['<header><h1>CMP 170HX — screening bench</h1>'
         f'<span class="sub">{len(runs)} run{"s" if len(runs) != 1 else ""} on record</span>'
         '</header>']
    b.append(live_panel(gs))
    if not runs:
        b.append('<div class="card"><p class="muted">No runs yet. '
                 'Screen a card with <code>bash hx-vram-screen.sh 15</code>.</p></div>')
    else:
        rows = "".join(
            f'<tr><td><a class="row" href="/run/{r["dir"]}"><strong>{html.escape(r["sn"])}'
            f'</strong></a></td>'
            f'<td class="muted">{r["when"]}</td><td>{pill(r["verdict"])}</td>'
            f'<td>{pill(r["provenance"])}</td><td>{pill(r["thermal"])}</td>'
            f'<td class="muted">{html.escape(r["plateau"])}</td>'
            f'<td><a class="btn" href="/run/{r["dir"]}/customer.html">report</a></td></tr>'
            for r in runs)
        b.append('<div class="card"><table><thead><tr><th>Serial</th><th>Screened</th>'
                 '<th>Verdict</th><th>Provenance</th><th>Thermal</th>'
                 '<th>Plateau core / HBM</th>'
                 f'<th></th></tr></thead><tbody>{rows}</tbody></table></div>')
    # The live panel updates itself every 3 s; the slow meta refresh is only a
    # no-JS safety net so the run table below cannot go stale for long.
    refresh = '<meta http-equiv="refresh" content="60">' if lv else ""
    b.append("<script>" + LIVE_JS + "</script>")
    return page("170HX screening bench", "".join(b), head_extra=refresh)


def run_page(d, customer=False):
    m = RUNDIR.match(d)
    if not m:
        return None
    rp = RESULTS / d / f"{m.group('sn')}.md"
    if not rp.exists():
        return None
    md = rp.read_text(errors="replace")
    rows = telemetry(d)
    body = md_to_html(md)
    # Replace the ASCII sparkline block with the real chart.
    body = re.sub(r"<pre>core .*?</pre>", chart(rows), body, flags=re.S)
    if customer:
        head, acts = "", ""
    else:
        head = '<a class="btn back" href="/">&larr; all cards</a>'
        acts = (f'<div class="actions">'
                f'<a class="btn primary" href="/run/{d}/customer.html">Open customer report</a>'
                f'<a class="btn" href="/run/{d}/report.md">Markdown</a>'
                f'<a class="btn" href="/run/{d}/raw/03-telemetry.csv">Telemetry CSV</a>'
                f'<a class="btn" href="/run/{d}/raw/SHA256SUMS">Hashes</a></div>')
    return page(f"170HX {m.group('sn')}", f'{head}<div class="card">{body}</div>{acts}')


# ------------------------------------------------------------------ serving
class H(BaseHTTPRequestHandler):
    server_version = "hx-dashboard"

    def log_message(self, *a):
        pass                                    # quiet: journald already has it

    def send(self, body, ctype="text/html; charset=utf-8", code=200, dl=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if dl:
            self.send_header("Content-Disposition", f'attachment; filename="{dl}"')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        try:
            if p == "/":
                return self.send(index_page())
            if p == "/api/status":
                return self.send(json.dumps({"live": live(), "runs": list_runs()}, indent=2),
                                 "application/json")
            if p == "/api/live":
                try:
                    gs = live_gpus()
                except Exception as e:
                    gs, err = [], str(e)
                else:
                    err = None
                return self.send(
                    json.dumps({"ts": time.time(), "gpus": gs, "error": err}),
                    "application/json; charset=utf-8")
            m = re.match(r"^/run/([A-Za-z0-9._-]+)(/.*)?$", p)
            if m:
                d, rest = m.group(1), (m.group(2) or "")
                dm = RUNDIR.match(d)
                if not dm or not (RESULTS / d).is_dir():
                    return self.send(page("Not found", "<p>Unknown run.</p>"), code=404)
                sn = dm.group("sn")
                if rest in ("", "/"):
                    return self.send(run_page(d) or "not found")
                if rest == "/customer.html":
                    return self.send(run_page(d, customer=True) or "not found")
                if rest == "/report.md":
                    f = RESULTS / d / f"{sn}.md"
                    if f.is_file():
                        return self.send(f.read_bytes(), "text/markdown; charset=utf-8",
                                         dl=f"170HX-{sn}.md")
                r = re.match(r"^/raw/([A-Za-z0-9._-]+)$", rest)
                if r:
                    f = (RESULTS / d / r.group(1)).resolve()
                    if RESULTS.resolve() in f.parents and f.is_file():
                        return self.send(f.read_bytes(), "text/plain; charset=utf-8", dl=f.name)
            return self.send(page("Not found", '<p>Nothing here. <a href="/">Back</a></p>'),
                             code=404)
        except Exception as e:          # a broken run must never take the whole page down
            return self.send(page("Error", f"<p>{html.escape(str(e))}</p>"), code=500)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    RESULTS.mkdir(parents=True, exist_ok=True)
    # Default loopback: this page has no login and serves full nvidia-smi -q
    # and dmesg excerpts. Expose it deliberately via HX_BIND=0.0.0.0 (bench
    # LAN) only if that trade-off is wanted.
    BIND = os.environ.get("HX_BIND", "127.0.0.1")
    print(f"hx-dashboard on {BIND}:{PORT}  serving {RESULTS}", flush=True)
    Server((BIND, PORT), H).serve_forever()
