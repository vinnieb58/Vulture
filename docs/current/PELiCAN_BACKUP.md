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

## Raven database snapshots (twice daily)

The daily **recovery bundle** above backs up `data/vulture.db` only (plus git, `.env`, config, and docs). It does **not** cover:

- Other Raven SQLite databases under `data/*.db` (for example Finch activity DBs)
- `data/kestrel/*.db`
- Nest operational JSON state (`data/kestrel_nest_history.jsonl`, `data/kestrel_nest_status.json`)
- Twice-daily scheduling

For dedicated database backups independent of deploy/update scripts, use the **database snapshot** job. It publishes compressed archives to:

```text
/mnt/storage/pelican_backup/raven-db-snapshots/
  raven-db-snapshot-YYYYMMDDTHHMMSSZ.tar.gz      # or .tar.zst when zstd(1) is installed
  raven-db-snapshot-YYYYMMDDTHHMMSSZ.tar.gz.sha256
  .pelican-staging/                              # transient; removed after success
```

Each snapshot includes:

| Source | Notes |
|--------|-------|
| `data/*.db` | Online SQLite backup + `PRAGMA integrity_check` per database |
| `data/kestrel/*.db` | Same SQLite-safe backup path |
| `data/kestrel_nest_history.jsonl` | Included only when present and below size limit (default 5 MiB) |
| `data/kestrel_nest_status.json` | Included only when present and below size limit |

`data/vulture.db` is required. Other databases are included when present. Failed SQLite backup or integrity check aborts the run without publishing a partial archive.

Retention keeps snapshots from the last **14 days** (default). Configure with `PELICAN_DB_SNAPSHOT_RETENTION_DAYS`.

### Manual command

```bash
cd /home/vinnieb58/projects/vulture
bash scripts/pelican_db_snapshot.sh
```

Optional overrides:

```bash
PELICAN_DB_SNAPSHOT_TARGET=/mnt/storage/pelican_backup/raven-db-snapshots \
PELICAN_DB_SNAPSHOT_RETENTION_DAYS=14 \
bash scripts/pelican_db_snapshot.sh
```

### Recognizing success

```text
pelican-db-snapshot: INFO: Pelican backup target verified
pelican-db-snapshot: INFO: Backed up data/vulture.db: SQLite backup integrity check passed
...
pelican-db-snapshot: INFO: Published database snapshot: /mnt/storage/pelican_backup/raven-db-snapshots/raven-db-snapshot-....tar.gz
pelican-db-snapshot: INFO: Pelican database snapshot completed successfully
```

Failures print `pelican-db-snapshot: ERROR:` lines and exit non-zero (same journald pattern as the recovery bundle).

### Twice-daily systemd timer

`pelican-db-snapshot.service` (oneshot) and `pelican-db-snapshot.timer` run at **~03:00** and **~15:00** local time (each with up to 15 minutes randomized delay). The oneshot service must **not** be enabled directly — only the timer.

```bash
cd /home/vinnieb58/projects/vulture
./scripts/install_pelican_db_snapshot_timer.sh --enable
```

Or enable the timer directly after units are installed:

```bash
sudo systemctl enable --now pelican-db-snapshot.timer
```

Confirm schedule:

```bash
systemctl list-timers pelican-db-snapshot.timer
journalctl -u pelican-db-snapshot.service -n 50 --no-pager
```

Disable without removing units:

```bash
sudo systemctl disable --now pelican-db-snapshot.timer
```

### Implementation files

- `scripts/pelican_db_snapshot.sh` — operator entry point
- `scripts/pelican_db_snapshot.py` — orchestrator
- `scripts/pelican/db_snapshot_*.py` — discovery, naming, retention
- `deploy/systemd/pelican-db-snapshot.service` — oneshot snapshot unit
- `deploy/systemd/pelican-db-snapshot.timer` — twice-daily timer
- `scripts/install_pelican_db_snapshot_timer.sh` — install/enable helper

Tests: `tests/test_pelican_db_snapshot.py`, `tests/test_pelican_db_snapshot_systemd_timer.py`
