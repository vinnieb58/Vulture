# Raven boot warnings — noise reduction (Priority 7)

Optional documentation for warnings observed on Raven after reboot. These are **not currently treated as critical** for Vulture/Crow production. Ignore them unless matching symptoms appear (instability, data loss, missing hardware, thermal throttling, etc.).

Related: `docs/current/RAVEN_RESTART_SURVIVAL_PLAN.md` (Priority 7 = optional / no action by default).

## Observed warnings (safe to ignore for now)

### PCIe bus error (Correctable)

Example:

```text
PCIe Bus Error: severity=Correctable, ...
```

**Meaning:** A correctable PCIe error was logged by the kernel. Often benign on consumer hardware.

**Recommendation:** Ignore unless you see repeated **Uncorrectable** errors, crashes, or device disconnects.

### i915 display polling warnings

Example:

```text
i915 ... [drm] ... polling ...
```

**Meaning:** Intel integrated graphics driver noise on headless or lightly used display paths.

**Recommendation:** Ignore on a headless server role unless display output is required and failing.

### Audio topology firmware warning

Example:

```text
... audio topology ... firmware ...
```

**Meaning:** Audio subsystem firmware/topology probe warning; common when audio hardware is unused.

**Recommendation:** Ignore unless audio I/O is part of Raven’s workload and is broken.

### thermald — no coretemp sysfs found

Example:

```text
thermald: No coretemp sysfs interface found
```

**Meaning:** `thermald` could not find Intel `coretemp` sensors; thermal daemon may be ineffective on this platform.

**Recommendation:** Ignore unless you rely on `thermald` for thermal management or see overheating symptoms. Monitor with other tools if needed.

### ModemManager warnings

Example:

```text
ModemManager[...]: ...
```

**Meaning:** ModemManager probing or modem-related dbus activity on a host that may not use a cellular modem.

**Recommendation:** Ignore if Raven does not use cellular/mobile broadband. Optional cleanup below.

## Optional cleanup — ModemManager only

If Raven **does not** use any cellular modem or mobile broadband USB device, you may reduce log noise by disabling ModemManager:

```bash
sudo systemctl disable --now ModemManager
```

### Warning

Only run this if you are certain Raven has **no** cellular modem or mobile broadband dependency. Disabling ModemManager can break LTE/5G USB modems and similar devices.

This repo does **not** disable ModemManager automatically during deploy or health checks.

## Verifying warnings after reboot

Use the health check scripts (Priority 6):

```bash
~/raven_healthcheck.sh --post-reboot
```

The **Recent boot warnings** section runs:

```bash
journalctl -b -p warning..alert --no-pager
```

Cross-check entries against this document before treating them as action items.
