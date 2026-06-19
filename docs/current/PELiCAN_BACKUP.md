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

## Implementation files

- `scripts/pelican_backup.sh` — operator entry point
- `scripts/pelican_backup.py` — orchestrator
- `scripts/pelican/` — testable helpers (naming, retention, mount validation, SQLite backup, manifest)

Tests: `tests/test_pelican_backup.py`
