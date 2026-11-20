#!/usr/bin/env python3
"""cardreport.py — CMP 170HX (GA100) card dossier for our toolkit.

Reads everything worth knowing about an installed card and renders a raw,
transparent report (no opaque grades):

  * physical VRAM   — FBP/FBPA/FBIO defect words -> latent GiB, per-HBM-stack
    breakdown (12 FBP = 96 GiB full bin; the per-stack table answers "is the
    6th stack live" for each card), current policy vs latent delta
  * compute         — GPC/TPC defect words -> latent SM count vs enabled
  * features        — NVLink mask, sys_pipe (MIG/ECC/NVDEC) mask, NVDEC/NVJPG
    status, PCIe Gen3/Gen4 disable bits, device-id words, lane status
  * uptime telemetry — NVML BBX cumulative driver-active hours (lifetime
    approximation) + current session hours
  * burn history    — SBFE state plus (with --ffrows) the physical FF row
    walk: rows 222-486 used/free, i.e. whether burned records exist on the
    card and how much of the append-only correction budget remains

All fuse-word reads are plain BAR0 MMIO reads of the fused/status/defective
register set (no fuse-macro commands, no kernel module — same mmap-of-sysfs
access as regio.py). --ffrows additionally walks the physical fuse rows via
the CMD_READ macro sequence; that issues controller commands, so it demands
an IDLE card and is opt-in.

Register addresses from community GA100 research (kate-land/ga100vfi capture
data, live-confirmed on CMP 170HX by the honzapatCZ cmp170hx-test project,
2026-11). No upstream code used.

Usage:
  sudo python3 cardreport.py                  # full report, all cards
  sudo python3 cardreport.py --ffrows         # + physical FF row readout
  python3 cardreport.py                       # NVML/health only (no BAR0)
  python3 cardreport.py --selftest            # offline math validation
"""
import argparse
import ctypes
import json
import os
import re
import subprocess
import sys

TOTAL_FBP, TOTAL_GPC, TPC_PER_GPC, SM_PER_TPC = 12, 8, 8, 2
BYTES_PER_FBP = {"8gb": 8, "10gb": 4}          # GiB per live FBP, by SKU
FF_ROW_MIN, FF_ROW_MAX = 222, 486              # append-only FF region

# --- BAR0 fuse word map (fused = factory OTP, status = live incl. FF overrides)
FIELDS = {   # name: (fused_off, status_off_or_None)
    "fbio_disable":     (0x0082036C, 0x00820C14),
    "fbpa_disable":     (0x00820368, 0x00820C18),
    "gpc_disable":      (0x00820350, 0x00820C1C),
    "tpc_gpc0_disable": (0x00820760, 0x00820C38),
    "tpc_gpc1_disable": (0x00820764, 0x00820C3C),
    "tpc_gpc2_disable": (0x00820768, 0x00820C40),
    "tpc_gpc3_disable": (0x0082076C, 0x00820C44),
    "tpc_gpc4_disable": (0x00820770, 0x00820C48),
    "tpc_gpc5_disable": (0x00820774, 0x00820C4C),
    "tpc_gpc6_disable": (0x00820778, 0x00820C50),
    "tpc_gpc7_disable": (0x0082077C, 0x00820C54),
    "pes_disable":      (0x00820690, None),
    "fbp_disable":      (0x00820364, 0x00820D38),
    "rop_l2_disable":   (0x008202C4, None),
    "nvlink_disable":   (0x00820684, 0x00820DB8),
    "sys_pipe_disable": (0x008203A4, 0x00820E30),
    "pcie_devida":      (0x008204D8, None),
    "pcie_devidb":      (0x0082056C, None),
}
DEFECTIVE = {  # name: offset  (physical defect words — the silicon truth)
    "fbio_defective":     0x008205D4,
    "fbpa_defective":     0x008205D0,
    "gpc_defective":      0x008205C4,
    "tpc_gpc0_defective": 0x008205C8,
    "tpc_gpc1_defective": 0x00820508,
    "tpc_gpc2_defective": 0x0082050C,
    "tpc_gpc3_defective": 0x00820514,
    "tpc_gpc4_defective": 0x00820564,
    "tpc_gpc5_defective": 0x008205AC,
    "tpc_gpc6_defective": 0x00820B84,
    "tpc_gpc7_defective": 0x00820B80,
    "pes_defective":      0x00820698,
    "fbp_defective":      0x008205CC,
    "rop_l2_defective":   0x008205E8,
    "nvlink_defective":   0x0082068C,
    "sys_pipe_defective": 0x008203AC,
}
BIT_FIELDS = {  # name: (reg_off, bit)
    "gen3_dis_bit1":     (0x00820250, 1),
    "gen3_dis_bit9":     (0x00820250, 9),
    "pcie_gen4_dis":     (0x00820520, 3),   # MAGIC_BITS_D[25]
    "boot_gen23_dis":    (0x0082057C, 6),
    "boot_gen3_dis":     (0x00820580, 7),
}
INFO = {  # name: offset
    "secured_burn_feature_enable": 0x00821108,  # SBFE shadow: 1 = FF burned
    "nvdec_disable":               0x00820378,
    "status_fb_config":            0x00820C34,
    "status_half_fbpa":            0x00820C00,
    "status_half_ltc":             0x00820C10,
    "status_nvdec":                0x00820C24,
    "status_nvjpg":                0x00820C0C,
    "status_pcie_lane":            0x00820C2C,
}
# fuse-macro (only used by --ffrows)
FUSECTRL, FUSEADDR, FUSEDATA, FUSEDEBUG = 0x820000, 0x820004, 0x820008, 0x820060
CMD_READ, IDLE_STATE = 0x1, 4


def sh(cmd, timeout=15):
    return subprocess.run(cmd, shell=isinstance(cmd, str), timeout=timeout,
                          capture_output=True, text=True, errors="replace")


def popcount(v):
    return bin(v).count("1")


# --- BAR0 access (same pattern as regio.py) --------------------------------
class Bar0:
    def __init__(self, pci_addr):
        import mmap
        path = "/sys/bus/pci/devices/%s/resource0" % pci_addr
        self.size = os.path.getsize(path) or 16 << 20
        self.fd = os.open(path, os.O_RDONLY | os.O_SYNC)
        self.mm = mmap.mmap(self.fd, self.size, mmap.MAP_SHARED, mmap.PROT_READ)

    def rd32(self, off):
        import struct
        return struct.unpack_from("<I", self.mm, off)[0]

    def close(self):
        self.mm.close()
        os.close(self.fd)


class Bar0RW(Bar0):
    """Read-write mapping, needed only for the fuse-macro row walk."""

    def __init__(self, pci_addr):
        import mmap
        path = "/sys/bus/pci/devices/%s/resource0" % pci_addr
        self.size = os.path.getsize(path) or 16 << 20
        self.fd = os.open(path, os.O_RDWR | os.O_SYNC)
        self.mm = mmap.mmap(self.fd, self.size, mmap.MAP_SHARED,
                            mmap.PROT_READ | mmap.PROT_WRITE)

    def wr32(self, off, val):
        import struct
        struct.pack_into("<I", self.mm, off, val)


def sane(v):
    return v is not None and v != 0 and v != 0xFFFFFFFF \
        and (v & 0xFFFF0000) != 0xBADF0000


def fuse_macro_rows(bar):
    """Walk physical fuse rows 0..511 via CMD_READ (controller commands!).

    Idle-asserted per row, FUSEADDR restored; any busy/poison aborts. The
    controller parks at 0xBADFxxxx after a failed command — if that happens,
    stop, cold-boot the card before further reads."""
    rows = {}
    orig = bar.rd32(FUSEADDR)
    try:
        for row in range(512):
            ctrl = bar.rd32(FUSECTRL)
            if ((ctrl >> 16) & 31) != IDLE_STATE or (ctrl & 3) or \
                    (ctrl & 0xFFFF0000) == 0xBADF0000:
                raise RuntimeError("fuse controller busy/poisoned before row "
                                   "%d (FUSECTRL=0x%08x) — stop, cold-boot"
                                   % (row, ctrl))
            if bar.rd32(FUSEDEBUG) != 0:
                raise RuntimeError("FUSEDEBUG=0x%08x before row %d"
                                   % (bar.rd32(FUSEDEBUG), row))
            bar.wr32(FUSEADDR, row)
            if bar.rd32(FUSEADDR) != row:
                raise RuntimeError("FUSEADDR did not latch row %d" % row)
            bar.wr32(FUSECTRL, CMD_READ)
            deadline = time.time() + 0.2
            while True:
                ctrl = bar.rd32(FUSECTRL)
                if ((ctrl >> 16) & 31) == IDLE_STATE and not (ctrl & 3):
                    break
                if time.time() > deadline:
                    raise RuntimeError("CMD_READ timeout row %d" % row)
            rows[row] = bar.rd32(FUSEDATA)
    finally:
        bar.wr32(FUSEADDR, orig)
    return rows


# --- NVML uptime telemetry --------------------------------------------------
def nvml_runtime():
    """{uuid: {lifetime_h, session_h}} — BBX cumulative driver-active time
    + current session. Stdlib ctypes only."""
    try:
        lib = ctypes.CDLL("libnvidia-ml.so.1")
    except OSError:
        return {}

    class T(ctypes.Structure):
        _fields_ = [("v", ctypes.c_uint)]

    def fn(name, *args):
        try:
            f = getattr(lib, name)
        except AttributeError:
            return None
        f.argtypes = list(args)
        f.restype = ctypes.c_int
        return f

    init = fn("nvmlInit_v2")
    count = fn("nvmlDeviceGetCount_v2", ctypes.POINTER(ctypes.c_uint))
    handle = fn("nvmlDeviceGetHandleByIndex_v2", ctypes.c_uint,
                ctypes.POINTER(ctypes.c_void_p))
    uuid = fn("nvmlDeviceGetUUID", ctypes.c_void_p,
              ctypes.POINTER(ctypes.c_char), ctypes.c_uint)
    bbx = fn("nvmlDeviceGetBBXTimeData_v1", ctypes.c_void_p, ctypes.POINTER(T))
    up = fn("nvmlDeviceGetGpuUptime_v1", ctypes.c_void_p, ctypes.POINTER(T))
    if not all((init, count, handle, uuid)):
        return {}
    if init() != 0:
        return {}
    out = {}
    try:
        n = ctypes.c_uint()
        if count(ctypes.byref(n)) != 0:
            return out
        for i in range(n.value):
            h = ctypes.c_void_p()
            if handle(i, ctypes.byref(h)) != 0:
                continue
            buf = ctypes.create_string_buffer(96)
            if uuid(h, buf, 96) != 0:
                continue
            entry = {}
            for q, key in ((bbx, "lifetime_h"), (up, "session_h")):
                if not q:
                    continue
                t = T()
                if q(h, ctypes.byref(t)) == 0 and t.v != 0xFFFFFFFF:
                    entry[key] = round(t.v / 3600.0, 1)
            out[buf.value.decode(errors="replace").lower()] = entry
    finally:
        lib.nvmlShutdown()
    return out


def smi(query):
    r = sh(["nvidia-smi", "--query-gpu=" + query, "--format=csv,noheader,nounits"])
    return [ln.split(",") for ln in r.stdout.splitlines()
            if ln.strip()] if r.returncode == 0 else []


# --- analytics ---------------------------------------------------------------
def analytics(words, sku):
    """words: {name: value} with *_disable (status preferred, fused fallback)
    and *_defective keys. Unreachable words (0/BADF/ffff) read as unknown,
    never as zero-defect — sections built on them are marked unavailable."""
    gib = BYTES_PER_FBP[sku]

    def word_ok(v):
        # 0 is a VALID fuse word (no defects / nothing disabled); only these
        # mean the register did not answer: all-ones and BADF poison.
        return v is not None and v != 0xFFFFFFFF and (v & 0xFFFF0000) != 0xBADF0000

    unreachable = [n for n, v in words.items() if not word_ok(v)]

    def r(name):
        v = words.get(name)
        return v if word_ok(v) else None

    def w(name):
        v = r(name)
        return 0 if v is None else v

    fbp_def, fbp_dis = w("fbp_defective"), w("fbp_disable")
    stacks = []
    for s in range(TOTAL_FBP // 2):
        d = popcount(fbp_def >> (2 * s) & 3)
        p = popcount(fbp_dis >> (2 * s) & 3)
        stacks.append({"stack": s + 1, "fbps": [2 * s, 2 * s + 1],
                       "physical_gib": (2 - d) * gib,
                       "dead_fbps": d, "disabled_extra": max(0, p - d)})

    gpc_def, gpc_dis = w("gpc_defective"), w("gpc_disable")
    latent_sm = enabled_sm = 0
    per_gpc = []
    for i in range(TOTAL_GPC):
        row = {"gpc": i}
        if gpc_def >> i & 1:
            row.update({"latent_sms": 0, "enabled_sms": 0, "dead_gpc": True})
        else:
            tpd = w("tpc_gpc%d_defective" % i)
            tps = w("tpc_gpc%d_disable" % i)
            lat = (TPC_PER_GPC - popcount(tpd & 0xFF)) * SM_PER_TPC
            en = 0 if (gpc_dis >> i & 1) else \
                (TPC_PER_GPC - popcount(tps & 0xFF)) * SM_PER_TPC
            latent_sm += lat
            enabled_sm += en
            row.update({"latent_sms": lat, "enabled_sms": en, "dead_gpc": False})
        per_gpc.append(row)

    used = None
    vram_ok = r("fbp_defective") is not None
    compute_ok = r("gpc_defective") is not None and \
        all(r("tpc_gpc%d_defective" % i) is not None for i in range(TOTAL_GPC))
    return {
        "sku": sku,
        "words_unreachable": unreachable,
        "vram": {
            "available": vram_ok,
            "latent_gib": (TOTAL_FBP - popcount(fbp_def & 0xFFF)) * gib if vram_ok else None,
            "policy_enabled_gib": (TOTAL_FBP - popcount(fbp_dis & 0xFFF)) * gib if vram_ok else None,
            "fbp_defective": "0x%x" % fbp_def if vram_ok else "unreachable",
            "fbp_disable": "0x%x" % fbp_dis,
            "fbpa_defective": ("0x%x" % w("fbpa_defective"))
                              if r("fbpa_defective") is not None else "unreachable",
            "fbio_defective": ("0x%x" % w("fbio_defective"))
                              if r("fbio_defective") is not None else "unreachable",
            "hbm_stacks": stacks if vram_ok else []},
        "compute": {
            "available": compute_ok,
            "latent_sms": latent_sm if compute_ok else None,
            "enabled_sms": enabled_sm if compute_ok else None,
            "gpc_defective": "0x%x" % gpc_def,
            "gpc_disable": "0x%x" % gpc_dis,
            "rop_l2_defective": ("0x%x" % w("rop_l2_defective"))
                                if r("rop_l2_defective") is not None else "unreachable",
            "pes_defective": ("0x%x" % w("pes_defective"))
                             if r("pes_defective") is not None else "unreachable",
            "per_gpc": per_gpc if compute_ok else []},
        "features": {
            "nvlink_defective": "0x%x" % w("nvlink_defective"),
            "nvlink_disable": "0x%x" % w("nvlink_disable"),
            "sys_pipe_defective": "0x%x" % w("sys_pipe_defective"),
            "sys_pipe_disable": "0x%x" % w("sys_pipe_disable"),
            "gen3_disabled": bool(words.get("gen3_dis_bit1") or
                                  words.get("gen3_dis_bit9")),
            "gen4_disabled_bit": bool(words.get("pcie_gen4_dis")),
            "boot_gen23_dis": bool(words.get("boot_gen23_dis")),
            "boot_gen3_dis": bool(words.get("boot_gen3_dis")),
            "device_id_words": ["0x%x" % w("pcie_devida"),
                                "0x%x" % w("pcie_devidb")]},
    }


def ff_budget(rows):
    used = [r for r in range(FF_ROW_MIN, FF_ROW_MAX + 1) if rows.get(r)]
    return {"rows_used": len(used), "rows_free":
            (FF_ROW_MAX - FF_ROW_MIN + 1) - len(used),
            "highest_used_row": max(used) if used else None}


# --- report rendering ---------------------------------------------------------
def render(card):
    ident = card.get("identity", {})
    L = ["# card dossier — %s" % (ident.get("uuid") or card.get("bdf", "?")), ""]
    L += ["## identity",
          "- model: %s   driver: %s" % (ident.get("name"), ident.get("driver")),
          "- uuid: %s" % ident.get("uuid"),
          "- serial: %s   pci: %s" % (ident.get("serial"), card.get("bdf")),
          "- vram visible to driver: %s MiB" % ident.get("vram_mib"), ""]
    up = card.get("uptime") or {}
    L += ["## uptime telemetry",
          "- lifetime (BBX driver-active): %s h" % up.get("lifetime_h", "unavailable"),
          "- current session: %s h" % up.get("session_h", "unavailable"), ""]
    h = card.get("health") or {}
    if h:
        L += ["## health",
              "- temp %s C   power %s W   ECC mode: %s" %
              (h.get("temp_c"), h.get("power_w"), h.get("ecc_mode")),
              "- Xid lines in dmesg: %s" % h.get("xid_count", "?"), ""]
    p = card.get("physical")
    if p:
        v = p["vram"]
        if v.get("available"):
            L += ["## physical VRAM (fuse defect words — silicon truth)",
                  "- latent: **%s GiB**    policy-enabled: %s GiB    driver-visible: %s MiB"
                  % (v["latent_gib"], v["policy_enabled_gib"], ident.get("vram_mib")),
                  "- fbp_defective %s   fbp_disable %s   fbpa_def %s   fbio_def %s"
                  % (v["fbp_defective"], v["fbp_disable"], v["fbpa_defective"],
                     v["fbio_defective"]),
                  "", "| HBM stack | physical GiB | dead FBP | extra policy-off |",
                  "|---|---|---|---|"]
            for s in v["hbm_stacks"]:
                L.append("| %d | %s | %d | %d |" % (s["stack"], s["physical_gib"],
                                                    s["dead_fbps"], s["disabled_extra"]))
        else:
            L += ["## physical VRAM",
                  "- unavailable — %s unreachable (card asleep or registers "
                  "fused off?)" % v["fbp_defective"]]
        c = p["compute"]
        if c.get("available"):
            L += ["", "## compute",
                  "- SMs: latent **%s**   enabled %s" % (c["latent_sms"], c["enabled_sms"]),
                  "- gpc_defective %s   gpc_disable %s   rop_l2_def %s   pes_def %s"
                  % (c["gpc_defective"], c["gpc_disable"], c["rop_l2_defective"],
                     c["pes_defective"])]
        else:
            L += ["", "## compute",
                  "- unavailable — defect words unreachable"]
        for g in c["per_gpc"]:
            if g["dead_gpc"]:
                L.append("- GPC %d: dead" % g["gpc"])
            elif g["latent_sms"] != g["enabled_sms"]:
                L.append("- GPC %d: %s latent / %s enabled (policy hides %s SMs)"
                         % (g["gpc"], g["latent_sms"], g["enabled_sms"],
                            g["latent_sms"] - g["enabled_sms"]))
        f = p["features"]
        L += ["", "## features",
              "- NVLink: defective %s, disabled %s (needs U11 bridge mod even if clear)"
              % (f["nvlink_defective"], f["nvlink_disable"]),
              "- sys_pipe (MIG/ECC/NVDEC): defective %s, disabled %s"
              % (f["sys_pipe_defective"], f["sys_pipe_disable"]),
              "- NVDEC: disable word 0x%x, status 0x%x" %
              (card.get("info", {}).get("nvdec_disable", 0),
               card.get("info", {}).get("status_nvdec", 0)),
              "- PCIe: gen3_disabled=%s  gen4_dis_bit=%s  boot_gen23_dis=%s  boot_gen3_dis=%s"
              % (f["gen3_disabled"], f["gen4_disabled_bit"],
                 f["boot_gen23_dis"], f["boot_gen3_dis"]),
              "- device-id words: %s" % f["device_id_words"]]
        info = card.get("info") or {}
        sbfe = info.get("secured_burn_feature_enable")
        b = card.get("ff_budget")
        if b:
            burned = "%d burned FF records present" % b["rows_used"] \
                if b["rows_used"] else "FF region BLANK (never burned)"
        else:
            burned = "run with --ffrows for the physical row walk"
        L += ["", "## burn history / info words",
              "- SBFE (secured_burn_feature_enable): %s — %s (NB: SBFE=1 is the "
              "factory/cold-boot default and is NOT proof of burning)"
              % (sbfe, "burn path enabled" if sbfe == 1 else "state: %s" % sbfe),
              "- actual burn evidence: %s" % burned,
              "- status_half_fbpa 0x%x   status_half_ltc 0x%x   status_fb_config 0x%x"
              % (info.get("status_half_fbpa", 0), info.get("status_half_ltc", 0),
                 info.get("status_fb_config", 0))]
        if b:
            L += ["", "## FF row budget (physical row walk)",
                  "- rows used %s / free %s   highest used row: %s"
                  % (b["rows_used"], b["rows_free"], b["highest_used_row"])]
    else:
        L += ["## physical silicon",
              "(not measured — %s)" % card.get("fuse_error",
              "run as root for BAR0 fuse reads"), ""]
    return "\n".join(L)


# --- selftest (offline math validation, blog-card reference numbers) ---------
SELFTEST = {
    # kate-land blog card: fbp_defective=0x840 (80GiB latent), disable=0x852 (64GiB)
    "fbp_defective": 0x840, "fbp_disable": 0x852,
    "fbpa_defective": 0x00C0330C & 0xFFFFFF, "fbio_defective": 0x00C0330C & 0xFFFFFF,
    "gpc_defective": 0x0, "gpc_disable": 0x0,
    "tpc_gpc0_defective": 0x80, "tpc_gpc1_defective": 0x40,
    "tpc_gpc2_defective": 0x20, "tpc_gpc3_defective": 0x10,
    "tpc_gpc4_defective": 0x8, "tpc_gpc5_defective": 0x4,
    "tpc_gpc6_defective": 0x2, "tpc_gpc7_defective": 0x1,
    "nvlink_defective": 0x0, "nvlink_disable": 0x7,
    "sys_pipe_defective": 0x0, "sys_pipe_disable": 0x2,
}


def selftest():
    a = analytics(SELFTEST, "8gb")
    v, c = a["vram"], a["compute"]
    assert v["available"] and c["available"]
    assert v["latent_gib"] == 80, v["latent_gib"]       # 12-2 dead FBP
    assert v["policy_enabled_gib"] == 64, v["policy_enabled_gib"]
    assert sum(s["physical_gib"] for s in v["hbm_stacks"]) == 80
    # stacks 3 and 4 hold the two dead FBPs (bits 6,11 of 0x840)
    dead = [s["stack"] for s in v["hbm_stacks"] if s["physical_gib"] == 0]
    assert dead == [], dead                              # 0x840: no fully dead stack
    partial = [s["stack"] for s in v["hbm_stacks"] if s["physical_gib"] == 8]
    assert sorted(partial) == [4, 6], partial            # bits 6 (stack4) and 11 (stack6)
    # SMs: 8 GPCs live, one TPC dead each -> (8-1)*2*8 = 112 latent
    assert c["latent_sms"] == 112, c["latent_sms"]
    print("selftest OK: 80 GiB latent / 64 policy, 112 latent SMs, "
          "half-dead stacks %s" % partial)
    return 0


# --- main ----------------------------------------------------------------------
GA100_DEVICES = {"0x20c2", "0x20c3"}   # CMP 170HX 8GB / 10GB


def norm_bdf(s):
    """'00000000:01:00.0' or '0000:01:00.0' -> '01:00.0' (domain stripped)."""
    return s.strip().split(":", 1)[1]


def nvidia_cards():
    """[(bdf, uuid, identity_fields, is_ga100)] via sysfs + nvidia-smi."""
    out = []
    smi_rows = smi("uuid,pci.bus_id,name,serial,driver_version,memory.total,"
                   "temperature.gpu,power.draw")
    by_pci = {}
    for row in smi_rows:
        if len(row) >= 8:
            by_pci[norm_bdf(row[1])] = [c.strip() for c in row]
    for dev in sorted(os.listdir("/sys/bus/pci/devices")):
        p = "/sys/bus/pci/devices/" + dev
        try:
            if open(p + "/vendor").read().strip() != "0x10de":
                continue
            cls = int(open(p + "/class").read(), 16) >> 16
            if cls not in (0x03,):
                continue
            dev_id = open(p + "/device").read().strip()
            row = by_pci.get(norm_bdf(dev), ["?"] * 8)
            out.append((dev, row[0].lower(),
                        {"uuid": row[0], "name": row[2], "serial": row[3],
                         "driver": row[4], "vram_mib": row[5],
                         "temp_c": row[6], "power_w": row[7],
                         "pci_device_id": dev_id},
                        dev_id in GA100_DEVICES))
        except (OSError, ValueError):
            continue
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pci-addr", help="limit to one card, e.g. 0000:03:00.0")
    ap.add_argument("--sku", default="8gb", choices=list(BYTES_PER_FBP))
    ap.add_argument("--ffrows", action="store_true",
                    help="also walk physical fuse rows (idle card only)")
    ap.add_argument("--out", help="write markdown report to this file")
    ap.add_argument("--json", help="write raw JSON to this file")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    runtime = nvml_runtime()
    dmesg = sh("dmesg").stdout.lower()
    cards = []

    for bdf, uuid, ident, is_ga100 in nvidia_cards():
        if a.pci_addr and bdf != a.pci_addr:
            continue
        card = {"bdf": bdf, "identity": ident,
                "uptime": runtime.get(uuid, {}),
                "health": {"temp_c": ident.get("temp_c"),
                           "power_w": ident.get("power_w"),
                           "ecc_mode": "?", "xid_count": dmesg.count("xid")}}
        try:
            if not is_ga100:
                raise RuntimeError("device 10de:%s (%s) — GA100 fuse map not "
                                   "applicable, fuse reads skipped"
                                   % (ident["pci_device_id"].lstrip("0x"),
                                      ident.get("name") or "non-GA100"))
            if os.geteuid() != 0:
                raise RuntimeError("not root — BAR0 fuse reads skipped")
            bar = Bar0(bdf)
            try:
                pmc = bar.rd32(0)          # PMC_BOOT_0 liveness gate
                if not sane(pmc) or (pmc & 0xFFFF0000) == 0xBADF0000:
                    raise RuntimeError("BAR0 not talking (PMC_BOOT_0=0x%08x) — "
                                       "card asleep/uninitialized?" % pmc)
                words = {}
                for name, (fused, status) in FIELDS.items():
                    fv, sv = bar.rd32(fused), (bar.rd32(status) if status else None)
                    words[name] = sv if sane(sv) else fv
                    words[name + "__fused"] = fv
                for name, off in DEFECTIVE.items():
                    words[name] = bar.rd32(off)
                for name, (reg, bit) in BIT_FIELDS.items():
                    words[name] = (bar.rd32(reg) >> bit) & 1
                info = {name: bar.rd32(off) for name, off in INFO.items()}
                card["physical"] = analytics(words, a.sku)
                card["info"] = info
                card["words_raw"] = {"%s" % k: "0x%x" % v
                                     for k, v in words.items() if sane(v)}
                if a.ffrows:
                    if sh(["nvidia-smi", "--query-compute-apps=pid",
                           "--format=csv,noheader"]).stdout.strip():
                        raise RuntimeError("GPU busy — ffrows needs an idle card")
                    bar.close()
                    bar = Bar0RW(bdf)
                    rows = fuse_macro_rows(bar)
                    card["ff_budget"] = ff_budget(rows)
                    card["ff_rows_raw"] = {str(r): "0x%08x" % v
                                           for r, v in rows.items() if v}
            finally:
                bar.close()
        except Exception as e:
            card["fuse_error"] = str(e)
        cards.append(card)

    if not cards:
        print("no NVIDIA cards found", file=sys.stderr)
        return 1
    doc = "\n\n".join(render(c) for c in cards)
    print(doc)
    if a.out:
        open(a.out, "w").write(doc + "\n")
    if a.json:
        open(a.json, "w").write(json.dumps(cards, indent=2, default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
