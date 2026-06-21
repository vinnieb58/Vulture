# Pelican v1 — Raven Recovery Bundle Backup

Pelican Step 1 creates a timestamped **Raven recovery bundle** on the Pelican backup volume (`/mnt/storage/pelican_backup`). The bundle captures enough state to rebuild Raven/Vulture after hardware loss or corruption.

**Security warning:** Each bundle includes a copy of `.env`. Anyone who can read the Pelican backup target can read Raven secrets (Discord tokens, API keys, database credentials, and similar). Restrict filesystem permissions on `/mnt/storage/pelican_backup` and treat published bundles like production secret material.

---

## Manual command (Raven)

From the Vulture repository root on Raven:

```bash
cd /home/vinnieb58/projects/vulture
bash scripts/pelican_backup.sh
```

Optional environment overrides:

```bash
PELICAN_RETENTION_COUNT=14 \
PELICAN_BACKUP_TARGET=/mnt/storage/pelican_backup \
bash scripts/pelican_backup.sh
```

Dry inspection without changing retention (after verifying a successful run):

```bash
bash scripts/pelican_backup.sh --skip-retention
```

The script is read-only with respect to production `.env`, SQLite, systemd, mounts, Samba, and Docker. It only writes backup artifacts under the Pelican target.

---

## Expected output location

Successful bundles are published directly under the Pelican target:

```text
/mnt/storage/pelican_backup/
  raven-recovery-YYYYMMDDTHHMMSSZ.tar.gz      # or .tar.zst when zstd(1) is installed
  raven-recovery-YYYYMMDDTHHMMSSZ.tar.gz.sha256
  raven-recovery-YYYYMMDDTHHMMSSZ.tar.gz.manifest
  .pelican-staging/                             # transient; removed after success
```

During an in-progress or failed run you may see:

```text
/mnt/storage/pelican_backup/.pelican-staging/
  raven-recovery-YYYYMMDDTHHMMSSZ.incomplete/
  raven-recovery-YYYYMMDDTHHMMSSZ.tar.gz.partial
```

Incomplete names use the `.incomplete` suffix or `.partial` extension and are **not** treated as successful backups.

---

## Recognizing success

Success looks like:

```text
pelican-backup: INFO: Pelican backup target verified
...
pelican-backup: INFO: SQLite backup integrity check passed
pelican-backup: INFO: Published recovery bundle: /mnt/storage/pelican_backup/raven-recovery-....tar.gz
pelican-backup: INFO: Archive sha256: <64-char hex>
pelican-backup: INFO: Pelican backup completed successfully
```

Exit code is `0`.

Failure prints `pelican-backup: ERROR:` lines and exits non-zero. Common causes:

- Pelican drive unplugged or automount placeholder only
- Missing required source (`.env`, `data/vulture.db`, repo root)
- SQLite backup or `PRAGMA integrity_check` failure

---

## Inspecting the manifest

Each published archive has a companion manifest beside it:

```bash
less /mnt/storage/pelican_backup/raven-recovery-YYYYMMDDTHHMMSSZ.tar.gz.manifest
```

Inside the extracted archive:

```bash
tar -tzf /mnt/storage/pelican_backup/raven-recovery-YYYYMMDDTHHMMSSZ.tar.gz | head
```

The bundle root contains `MANIFEST.txt` (captured at archive creation time), git recovery metadata, repository tree, database copy, secrets, host config snapshots, and recovery docs.

Manifest fields include:

- backup timestamp and hostname
- script version
- git branch and commit
- SQLite integrity result
- source paths
- included files
- optional files recorded as missing
- final archive checksum (in the companion `.manifest` file)

Secret values are never written to manifests or logs.

---

## Verifying the archive checksum

```bash
ARCHIVE=/mnt/storage/pelican_backup/raven-recovery-YYYYMMDDTHHMMSSZ.tar.gz
sha256sum -c "${ARCHIVE}.sha256"
```

Expected output:

```text
raven-recovery-YYYYMMDDTHHMMSSZ.tar.gz: OK
```

Compare the hash with the `archive_sha256` field in the companion `.manifest` file.

---

## Basic manual restore inspection (non-destructive)

Inspect without overwriting production:

```bash
WORKDIR=/tmp/pelican-inspect-$$
mkdir -p "$WORKDIR"
cd "$WORKDIR"

ARCHIVE=/mnt/storage/pelican_backup/raven-recovery-YYYYMMDDTHHMMSSZ.tar.gz
tar -xzf "$ARCHIVE"
ls -la raven-recovery-*/
cat raven-recovery-*/MANIFEST.txt
cat raven-recovery-*/database/integrity_check.txt
git clone raven-recovery-*/git/vulture.bundle vulture-from-bundle
```

Do **not** copy `secrets/.env` or `database/vulture.db` back into production until you intend a real restore. For a read-only sanity check:

```bash
sqlite3 raven-recovery-*/database/vulture.db "PRAGMA integrity_check;"
git -C vulture-from-bundle log -1 --oneline
```

---

## Bundle contents summary

| Path in bundle | Purpose |
|----------------|---------|
| `git/` | Branch, commit, status, remotes, recent log, `vulture.bundle` |
| `repository/` | Tracked source/config plus filtered untracked operational files |
| `database/vulture.db` | Online SQLite backup with integrity result |
| `secrets/.env` | Restricted copy (`0600`) — treat as secret |
| `config/systemd-repo/` | Repo unit definitions (`deploy/systemd/`) |
| `config/systemd-installed/` | Installed Aviary units from `/etc/systemd/system/` when present |
| `config/docker-compose/` | Compose files from repo |
| `config/host/etc/fstab` | Host fstab when present |
| `config/samba/` | Samba configs when present |
| `docs/` | Recovery/manager/operations documentation discovered in repo |
| `MANIFEST.txt` | Human-readable inventory captured at archive time |

---

## Required vs optional sources

**Required (backup fails if missing or invalid):**

- Vulture repository root
- `/home/vinnieb58/projects/vulture/data/vulture.db` (online backup + integrity check)
- `/home/vinnieb58/projects/vulture/.env`
- Real Pelican mount with backing device (not empty autofs placeholder / root filesystem alias)

**Optional (recorded as missing in manifest; backup continues):**

- `/etc/fstab`
- `/etc/samba/smb.conf` and included Samba configs
- Installed Aviary systemd units under `/etc/systemd/system/` (Vulture, Finch, Kestrel, etc.)

---

## Retention

Default retention keeps **14** newest completed bundles matching `raven-recovery-*.tar.{gz,zst}`. The newest successful bundle is never deleted. Retention runs only after a fully successful publish; failed runs delete nothing.

Configure with `PELICAN_RETENTION_COUNT`.

---

## Mount verification behavior

The script does **not** trust directory existence alone. It:

1. Accesses `/mnt/storage/pelican_backup` to trigger automount (with timeout)
2. Uses `findmnt` to confirm a real backing filesystem/device
3. Rejects autofs placeholders (`systemd-1`, `autofs`)
4. Rejects targets that resolve to the same source as `/` (empty mountpoint on root FS)

---

## Daily systemd timer (Step 2)

Pelican backups can run automatically via `pelican-backup.service` (oneshot) and `pelican-backup.timer` (daily ~3:00 AM local time with up to 15 minutes randomized delay).

Unit definitions live in `deploy/systemd/`. The service executes the existing backup script only; it does not duplicate backup logic. The oneshot service has **no `[Install]` section** and must **not** be enabled directly — only the timer is enabled.

Deploy installs unit files but **does not** enable the timer or run a backup automatically.

### Install or update units on Raven

From the repo root:

```bash
cd /home/vinnieb58/projects/vulture
git pull

# Copy units + daemon-reload (no backup run):
./scripts/install_pelican_timer.sh

# Or install and enable the daily timer:
./scripts/install_pelican_timer.sh --enable
```

Full/quick Raven deploy (`scripts/update_raven.sh` / `scripts/update_raven_quick.sh`) also copies all `deploy/systemd/*.service` and `*.timer` files and runs `daemon-reload`, but still does **not** enable or start the Pelican timer unless you run the install script with `--enable`.

### Enable and start the timer

```bash
sudo systemctl enable --now pelican-backup.timer
```

### Confirm timer state and next run

```bash
systemctl is-enabled pelican-backup.timer
systemctl is-active pelican-backup.timer
systemctl list-timers --all | grep pelican
```

Expected:

- `pelican-backup.timer`: **enabled**, **active**
- `pelican-backup.service`: **disabled** (or `static`), **inactive** between runs

Show next scheduled trigger:

```bash
systemctl list-timers pelican-backup.timer
```

### Manually trigger one backup through systemd

```bash
sudo systemctl start pelican-backup.service
```

This runs the same script as a manual shell invocation. Exit status propagates to systemd (`SuccessExitStatus` is not overridden — non-zero script exit fails the unit).

### Inspect logs and last result

```bash
systemctl status pelican-backup.service --no-pager -l
journalctl -u pelican-backup.service -n 100 --no-pager
journalctl -u pelican-backup.service -b --no-pager
```

Success lines include `pelican-backup: INFO: Pelican backup completed successfully`. Failures include `pelican-backup: ERROR:` and a non-zero unit result.

### Disable the timer safely

```bash
sudo systemctl disable --now pelican-backup.timer
```

This stops scheduled backups without removing unit files. To remove installed units:

```bash
sudo rm -f /etc/systemd/system/pelican-backup.service /etc/systemd/system/pelican-backup.timer
sudo systemctl daemon-reload
```

### Boot and optional-storage behavior

The service has **no** `network-online` dependency and **no** hard mount requirement in systemd. If the Pelican drive is unavailable, the backup script fails cleanly and logs to journald; Raven boot is not blocked.

---

## Implementation files

- `scripts/pelican_backup.sh` — operator entry point
- `scripts/pelican_backup.py` — orchestrator
- `scripts/pelican/` — testable helpers (naming, retention, mount validation, SQLite backup, manifest)
- `deploy/systemd/pelican-backup.service` — oneshot backup unit
- `deploy/systemd/pelican-backup.timer` — daily timer
- `scripts/install_pelican_timer.sh` — install/enable helper

Tests: `tests/test_pelican_backup.py`, `tests/test_pelican_systemd_timer.py`

---

## Pelican backup monitoring

Pelican backup **health monitoring** runs separately from Canary's 5-minute infrastructure checks.

| Component | Cadence | Role |
|-----------|---------|------|
| **Canary** | Every 5 minutes | Storage mount, services, network, Docker — reads last backup monitor snapshot only |
| **pelican-monitor.timer** | Every 6 hours | Runs backup checksum/staleness/timer checks and sends Discord alerts |

Pelican currently monitors one backup definition: **Raven recovery bundles** (`raven_recovery`). Future backup types (Time Machine, Windows backups, full-image freshness) register as additional Pelican backup definitions in `pelican_monitor/definitions.py` — not separate timers.

### Install and enable the monitor timer

```bash
cd /home/vinnieb58/projects/vulture
./scripts/install_pelican_monitor_timer.sh --enable
```

Install only (no enable):

```bash
./scripts/install_pelican_monitor_timer.sh
sudo systemctl enable --now pelican-monitor.timer
```

Full/quick Raven deploy copies the units but does **not** enable the monitor timer unless you run the install script with `--enable`.

### Timer schedule

- **Unit:** `pelican-monitor.timer` → `pelican-monitor.service`
- **Schedule:** `OnCalendar=*-*-* 00,06,12,18:00:00` (every six hours)
- **RandomizedDelaySec:** `15m`
- **Persistent:** `true` (missed runs execute after Raven returns online)
- **Service:** oneshot, normally inactive between runs; do **not** enable `pelican-monitor.service` directly

### Inspect timer and service state

```bash
systemctl is-enabled pelican-monitor.timer
systemctl is-active pelican-monitor.timer
systemctl list-timers pelican-monitor.timer
systemctl status pelican-monitor.service --no-pager -l
journalctl -u pelican-monitor.service -n 100 --no-pager
```

### Aggregate backup status

```bash
cat data/backup_monitor_status.json | python3 -m json.tool
python3 -m json.tool data/backup_monitor_status.json | jq '.backups.raven_recovery'
```

Alert dedup state (shared Canary helper, keyed by backup ID):

```bash
cat data/canary_alert_state.json | python3 -m json.tool
```

Canary surfaces the snapshot (without re-running checks) at `checks.backup_monitor` in `data/canary_status.json`.

### Manually trigger one monitor run

```bash
sudo systemctl start pelican-monitor.service
# or from repo root:
bash scripts/pelican_monitor.sh --json
python3 -m pelican_monitor --json
```

Set Discord webhook for alerts (repo `.env` or export):

```bash
export PELICAN_MONITOR_DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."  # fallback
```

### Configuration variables

**Generic monitor:**

| Variable | Default | Purpose |
|----------|---------|---------|
| `PELICAN_MONITOR_STATUS_PATH` | `data/backup_monitor_status.json` | Aggregate JSON output |
| `PELICAN_MONITOR_ALERT_STATE_PATH` | `data/canary_alert_state.json` | Alert dedup state |
| `PELICAN_MONITOR_DISCORD_WEBHOOK_URL` | (empty) | Discord alerts; falls back to `DISCORD_WEBHOOK_URL` |
| `PELICAN_MONITOR_ENABLED_BACKUPS` | (all registered) | Comma-separated backup IDs to check |

**Raven recovery bundle (first definition):**

| Variable | Default | Purpose |
|----------|---------|---------|
| `PELICAN_RAVEN_RECOVERY_TARGET` | `/mnt/storage/pelican_backup` | Completed bundle directory |
| `PELICAN_RAVEN_RECOVERY_TIMER_UNIT` | `pelican-backup.timer` | Backup scheduler timer |
| `PELICAN_RAVEN_RECOVERY_SERVICE_UNIT` | `pelican-backup.service` | Backup oneshot service |
| `PELICAN_RAVEN_RECOVERY_WARN_HOURS` | `30` | Warning when approaching stale |
| `PELICAN_RAVEN_RECOVERY_CRITICAL_HOURS` | `36` | Critical staleness threshold |

### Safe failure simulation (no backup deletion)

**Simulate stale backup:**

```bash
PELICAN_RAVEN_RECOVERY_CRITICAL_HOURS=0.001 bash scripts/pelican_monitor.sh --json
```

**Simulate checksum failure** — rename sidecar temporarily:

```bash
sudo mv /mnt/storage/pelican_backup/raven-recovery-NEWEST.tar.zst.sha256 /tmp/pelican-test.sha256.bak
sudo systemctl start pelican-monitor.service
sudo mv /tmp/pelican-test.sha256.bak /mnt/storage/pelican_backup/raven-recovery-NEWEST.tar.zst.sha256
```

### Alert behavior

State-change Discord alerts only (no repeat every six hours):

- healthy → warning/critical
- warning → critical
- material reason change (issue code fingerprint)
- warning/critical → healthy recovery

**Critical example:**

```text
**Raven / Pelican backup CRITICAL**
Latest recovery bundle is 40 hours old (threshold 36h)
timer pelican-backup.timer: enabled=enabled, active=active, next=...
service pelican-backup.service: active=inactive, result=success, exit=0
latest backup: raven-recovery-20260618T030015Z.tar.zst, age=40.0h
host: raven
```

**Recovery example:**

```text
**Raven / Pelican backup RECOVERED**
Pelican backup monitoring returned to healthy.
...
host: raven
```

Messages never include `.env` contents, manifest bodies, or secret values.

### Disable monitor safely

```bash
sudo systemctl disable --now pelican-monitor.timer
sudo rm -f /etc/systemd/system/pelican-monitor.service /etc/systemd/system/pelican-monitor.timer
sudo systemctl daemon-reload
```

### Implementation files (monitoring)

- `pelican_monitor/` — registry, runner, Raven recovery checker
- `canary/alerting.py` — shared Discord delivery + dedup
- `scripts/pelican_monitor.sh` — systemd entry wrapper
- `scripts/install_pelican_monitor_timer.sh` — install/enable helper
- `deploy/systemd/pelican-monitor.service` — oneshot monitor unit
- `deploy/systemd/pelican-monitor.timer` — six-hour timer
- `tests/test_pelican_monitor.py`, `tests/test_pelican_monitor_systemd.py`

