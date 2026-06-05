# Raven restart survival plan

Checklist for keeping Raven healthy across reboots, power loss, and deploys. Vulture bot and scheduler run as **systemd services** (`vulture-bot`, `vulture-scheduler`); tmux is **not** part of normal production runtime.

See also:

- `docs/current/RAVEN_SYSTEMD_RUNTIME.md` — unit files, deploy, verification
- `docs/current/RAVEN_BOOT_WARNINGS.md` — optional noise reduction (Priority 7)

## Priorities

| Priority | Topic | Status |
|----------|-------|--------|
| 1 | systemd units installed and **enabled** for `vulture-bot` and `vulture-scheduler` | Required |
| 2 | `.env` and repo layout present under `/home/vinnieb58/projects/vulture` | Required |
| 3 | Tailscale (`tailscaled`) up after reboot | Required |
| 4 | SSH reachable; Samba (`smbd`) if file shares are used | Required |
| 5 | Docker dashboard container (read-only) if used — separate from bot/scheduler | As deployed |
| **6** | **Repo-tracked health check scripts** | **Implemented** |
| **7** | **Optional boot-warning cleanup / noise reduction** | **Optional — no action unless you choose** |

## Priority 6 — Health check scripts (implemented)

Scripts live in the repo and can be copied to the home directory on Raven.

### Install on Raven

From the Vulture repo root:

```bash
cd /home/vinnieb58/projects/vulture
cp scripts/raven_healthcheck.sh ~/raven_healthcheck.sh
chmod +x ~/raven_healthcheck.sh

# Optional post-reboot wrapper:
cp scripts/raven_post_reboot_check.sh ~/raven_post_reboot_check.sh
chmod +x ~/raven_post_reboot_check.sh
```

### Run

Full health report (all sections, OK/WARN/FAIL summary):

```bash
~/raven_healthcheck.sh
```

Focused post-reboot checklist (same core checks, tighter flow):

```bash
~/raven_healthcheck.sh --post-reboot
# or
~/raven_post_reboot_check.sh
```

### What the scripts check

- Host identity, uptime, last boot
- Failed systemd units
- Key services: SSH, Tailscale, Samba, Docker, **vulture-bot**, **vulture-scheduler**
- Network, routes, internet ping
- Disk, block devices, fstab, USB
- Docker and Samba status
- **Vulture via systemd** (status + journal) — not tmux
- Process fallbacks (`discord_bot.py`, `main.py`, etc.)
- Optional **tmux listing labeled debug-only** (missing sessions are not failures)
- Listening ports, recent boot warnings
- Final OK / WARN / FAIL summary

The scripts are read-only: they do not print `.env`, restart services, or change configuration.

### After a reboot

1. Wait for the host to finish booting.
2. Run `~/raven_healthcheck.sh --post-reboot` (or the wrapper).
3. Confirm `vulture-bot` and `vulture-scheduler` are **active** and enabled.
4. If either unit failed, inspect `journalctl -u <unit> -n 100` before restarting manually.

## Priority 7 — Optional boot-warning cleanup

Some kernel and userspace warnings appear in `journalctl -b` on Raven but are **not currently critical**. Details and optional ModemManager cleanup are documented in `docs/current/RAVEN_BOOT_WARNINGS.md`.

**Recommendation:** ignore unless symptoms appear. Do **not** disable ModemManager unless Raven has no cellular modem or mobile broadband device.

## Quick reference — production runtime

```bash
systemctl is-enabled vulture-bot vulture-scheduler
systemctl is-active vulture-bot vulture-scheduler
systemctl status vulture-bot --no-pager -l
systemctl status vulture-scheduler --no-pager -l
```

tmux may still be used for manual debugging; health checks must not treat absent tmux sessions as production failures.
