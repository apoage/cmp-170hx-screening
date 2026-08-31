# Bench setup

Everything here happens **before** the first card can be screened. It is
one-time work per bench, not per card.

The screening suite itself is ours. The unlock is not — see
[Prior work](#prior-work-the-unlock-is-not-ours) below.

---

## 1. The physical bench

The CMP 170HX is a **passively cooled** card with no onboard fan and no fan
sensor. It cannot regulate its own temperature, and `nvidia-smi` will never tell
you how much air it is getting. Everything the thermal half of this method
measures depends on that airflow being **constant across the whole batch**.

- **Fixed airflow, not a curve.** Do not let the motherboard manage the fans.
  On a typical board the BIOS fan curve follows *CPU* temperature, so it idles
  the fans through an entire GPU soak. Set a fixed duty cycle and leave it.
  Constant airflow is what makes °C/W comparable card-to-card; a curve that
  reacts differently on different runs destroys the comparison silently.
- **Power high-RPM fans from the PSU, not the header.** Server-grade fans draw
  more than a fan header can supply and trip its overcurrent protection — the
  visible symptom is a ramp-cut-ramp cycle. Take 12 V from a molex, and use the
  header only for PWM and for **one** tach signal (tach lines cannot be
  paralleled — two fans on one tach reads as garbage).
- Fan control on most consumer boards needs the `nct6775` module
  (`echo nct6775 >> /etc/modules`). Check your Super I/O chip first.
- **Never resolve hwmon by index.** `/sys/class/hwmon/hwmon0` is not stable
  across reboots — ours swapped between `hwmon0` and `hwmon1`. Always match on
  `/sys/class/hwmon/*/name`.

## 2. Driver

Screening needs the card to be visible to the driver with its full heap
addressable. We validated **nvidia-open 610.43.03**, installed from the `.run`
with `--dkms --kernel-module-type=open`.

Gotchas that cost us time:

- The `.run` installer **refuses to run while nvidia modules are loaded**, and
  `modprobe -r` stops working once the distro packages are purged. Unload with
  `rmmod` in order: `nvidia_uvm`, `nvidia_drm`, `nvidia_modeset`, `nvidia`.
- Anything holding `/dev/nvidia-uvm` blocks that unload. `nvtop` is a common
  culprit; so is any inference server.
- Purging the distro nvidia packages also removes the **nouveau blacklist**, so
  a secondary display card can fall back to nouveau and vanish from
  `nvidia-smi`. Harmless — it keeps the console alive — but do not mistake it
  for a dead card.
- Secure Boot must be off, or the DKMS modules will not load.

## 3. Unlock

Run `bash hx-install-unlock.sh` (as root) **with a 170HX already fitted**, then
**cold boot** — power off, cut power for ~60 s, power on. A warm reboot does not
reset the PCIe PHY, so the link retrain does not take.

The script exists because `cmpunlocker`'s own `install.sh` hard-aborts when no
CMP card is on the bus, so the unlock **cannot be staged on an empty bench**.
Ours checks the card is present, Secure Boot is off, headers are installed and
no X server is running, then runs the driver install and the unlock in the order
that works.

> **Re-run `cmpunlocker` after every kernel upgrade.** A DKMS rebuild replaces
> the patched modules with stock ones and the card quietly reverts.

Verify afterwards:

```console
$ nvidia-smi --query-gpu=name,memory.total --format=csv
name, memory.total [MiB]
NVIDIA CMP 170HX, 65536 MiB
```

## 4. Install the suite

```console
$ bash install.sh --service
```

See the top-level [README](../README.md).

---

## Credit

The unlock is **[cmpunlocker](https://github.com/amoghmunikote/cmpunlocker)**
(GPL-2.0) — somebody else's work. We did not write it and do not redistribute
it; this repository contains none of its code, and `hx-install-unlock.sh` only
*calls* it, in the right order, with guards for the failure modes we hit.

This page exists so you can get a bench to the starting line. The suite that
this repository is actually about begins once the card is unlocked — see
[docs/METHODOLOGY.md](../docs/METHODOLOGY.md).
