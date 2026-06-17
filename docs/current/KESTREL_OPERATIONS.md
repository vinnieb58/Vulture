# Kestrel operations (Smart Meter Texas)

Kestrel is a **read-only**, isolated household energy probe. It does not modify Vulture hunt scheduling, Crow commands, or hunt runtime.

## Manual refresh

```bash
cd /home/vinnieb58/projects/vulture
source .venv/bin/activate

# Recommended manual API refresh (3 completed local days)
python3 experiments/kestrel/smart_meter_texas_probe.py --live-refresh --days 3 --no-browser-fallback

# Summary from SQLite
python3 experiments/kestrel/smart_meter_texas_probe.py --summary-only
```

Requires `KESTREL_ENABLED=true` and Smart Meter Texas credentials in repo-root `.env`.

## Daily systemd timer (v0)

Isolated units (separate from Vulture scheduler):

| Unit | Purpose |
|------|---------|
| `kestrel-smt-refresh.service` | Oneshot API live refresh (`--days 3 --no-browser-fallback`) |
| `kestrel-smt-refresh.timer` | Daily trigger (~08:30 local, 15m randomized delay) |

Reference files: `deploy/systemd/kestrel-smt-refresh.service`, `deploy/systemd/kestrel-smt-refresh.timer`.

### Install

```bash
cd /home/vinnieb58/projects/vulture
chmod +x scripts/install_kestrel_timer.sh

# Copy units + daemon-reload
./scripts/install_kestrel_timer.sh

# Copy units, daemon-reload, enable timer
./scripts/install_kestrel_timer.sh --enable
```

Or manually:

```bash
sudo cp deploy/systemd/kestrel-smt-refresh.service /etc/systemd/system/
sudo cp deploy/systemd/kestrel-smt-refresh.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kestrel-smt-refresh.timer
systemctl list-timers --all | grep kestrel
```

### Manual one-shot test

```bash
sudo systemctl start kestrel-smt-refresh.service
journalctl -u kestrel-smt-refresh.service -n 100 --no-pager
python3 experiments/kestrel/smart_meter_texas_probe.py --summary-only
```

### Timer status

```bash
systemctl status kestrel-smt-refresh.timer
systemctl list-timers --all | grep kestrel
```

### Disable / rollback

```bash
sudo systemctl disable --now kestrel-smt-refresh.timer
sudo rm -f /etc/systemd/system/kestrel-smt-refresh.service /etc/systemd/system/kestrel-smt-refresh.timer
sudo systemctl daemon-reload
```

## Exit behavior

The probe exits **0** on `ok` or `partial` refresh status, **1** on `failed` or `unsupported`. Timer runs use API-only mode (no Playwright).

## Data paths

- SQLite: `data/kestrel/kestrel.db` (or `KESTREL_DB_PATH`)
- Status JSON: `data/kestrel/kestrel_status.json`

## Safety

- Read-only probe — no writes to Smart Meter Texas
- Credentials and raw ESIIDs are never logged or printed
- Browser fallback is disabled for unattended timer runs
- Do not commit `.env`, CSV exports, or `data/kestrel/*.db`

See also: `experiments/kestrel/README.md`
