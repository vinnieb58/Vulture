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

## Canary monitoring

Canary (v0.2+) monitors Pelican backup health on each check cycle and can send **Discord alerts** when backup health changes. This complements the existing storage-volume check (`/mnt/storage/pelican_backup` mounted) with timer, service-result, archive freshness, and checksum validation.

### Configuration variables

Set in `docker-compose.canary.yml` environment or repo `.env`:

| Variable | Default | Purpose |
|----------|---------|---------|
| `CANARY_PELICAN_STALE_HOURS` | `36` | Critical when the newest completed archive is older than this |
| `CANARY_PELICAN_STALE_WARN_HOURS` | `30` | Warning when approaching stale (below critical threshold) |
| `CANARY_PELICAN_BACKUP_TARGET` | `/mnt/storage/pelican_backup` | Directory scanned for `raven-recovery-*` bundles |
| `CANARY_PELICAN_TIMER_UNIT` | `pelican-backup.timer` | Timer unit to inspect |
| `CANARY_PELICAN_SERVICE_UNIT` | `pelican-backup.service` | Oneshot service for last-run result |
| `CANARY_DISCORD_WEBHOOK_URL` | (empty) | Webhook for Pelican alerts; falls back to `DISCORD_WEBHOOK_URL` |

### Inspect current monitoring state

```bash
# Full Canary snapshot (includes checks.pelican_backup)
cat data/canary_status.json | python3 -m json.tool

# Pelican section only
python3 -m json.tool data/canary_status.json | jq '.checks.pelican_backup'

# Alert dedup / last notified state
cat data/canary_alert_state.json
```

Healthy Pelican backup reports `"status": "ok"` with timer active, service last result success, real mount, valid checksum, and archive age below threshold.

### Manually run the Pelican check

One-shot JSON (host paths; use `CANARY_HOST_ROOT=/host` inside Canary container):

```bash
cd /home/vinnieb58/projects/vulture
CANARY_HOST_ROOT=/host python3 -m canary.pelican_backup
```

Or trigger a full Canary cycle (updates status + evaluates Discord alerts):

```bash
docker compose -f docker-compose.canary.yml restart canary
tail -f logs/canary.log
```

### Safe failure simulation (no backup deletion)

These tests do not remove real recovery bundles:

**Simulate stale backup alert** — temporarily lower the stale threshold:

```bash
CANARY_PELICAN_STALE_HOURS=0.001 CANARY_HOST_ROOT=/host python3 -m canary.pelican_backup
```

Expect `"status": "critical"` and issue code `BACKUP_STALE`. Restore default threshold before leaving Canary running with alerts enabled.

**Simulate timer failure** — disable timer briefly (re-enable after test):

```bash
sudo systemctl disable --now pelican-backup.timer
# wait for next Canary cycle or run manual check
sudo systemctl enable --now pelican-backup.timer
```

**Simulate checksum failure** — rename sidecar temporarily (do not modify the archive):

```bash
sudo mv /mnt/storage/pelican_backup/raven-recovery-NEWEST.tar.zst.sha256 /tmp/pelican-test.sha256.bak
# run check / wait for Canary
sudo mv /tmp/pelican-test.sha256.bak /mnt/storage/pelican_backup/raven-recovery-NEWEST.tar.zst.sha256
```

Replace `NEWEST` with the actual newest bundle stamp from `ls /mnt/storage/pelican_backup/raven-recovery-*.tar.zst`.

### Confirm duplicate alert suppression

1. Ensure Canary is unhealthy (e.g. stale threshold trick above) with webhook configured.
2. Note the first Discord alert.
3. Wait for two or more Canary intervals (~10 minutes at default 300s).
4. Confirm **no repeated identical alerts**; `data/canary_alert_state.json` should show stable `fingerprint` and `severity`.

### Confirm recovery alerting

1. From an alerting state, restore health (re-enable timer, restore checksum, reset threshold).
2. After the next Canary cycle, expect **one** Discord message containing `Pelican backup RECOVERED`.
3. Further healthy cycles should not send additional recovery messages.

### Expected Discord messages

**Critical alert example:**

```text
**Raven / Pelican backup CRITICAL**
Latest backup is 40.0h old (threshold 36h)
timer pelican-backup.timer: enabled=enabled, active=active, next=...
service pelican-backup.service: active=inactive, result=success, exit=0
latest backup: raven-recovery-20260618T030015Z.tar.zst, age=40.0h
host: raven
```

**Recovery example:**

```text
**Raven / Pelican backup RECOVERED**
Pelican backup monitoring returned to healthy.
timer pelican-backup.timer: enabled=enabled, active=active, next=...
service pelican-backup.service: active=inactive, result=success, exit=0
latest backup: raven-recovery-20260620T030015Z.tar.zst, age=12.0h
host: raven
```

Messages never include `.env` contents, manifest bodies, or secret values.

### Implementation files (monitoring)

- `canary/pelican_backup.py` — Pelican health check logic
- `canary/alerting.py` — Discord delivery + dedup state
- `canary/checks.py` — registers `pelican_backup` section
- `tests/test_canary_pelican.py` — focused monitoring tests

