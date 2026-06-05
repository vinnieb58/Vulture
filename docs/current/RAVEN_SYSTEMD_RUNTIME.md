# Raven production runtime (systemd)

Raven runs Vulture bot and scheduler as **systemd user services**, not tmux sessions.

## Services

| Unit | Process | Purpose |
|------|---------|---------|
| `vulture-bot.service` | `discord_bot.py` | Discord control plane (Crow + Vulture hunt commands) |
| `vulture-scheduler.service` | `main.py` (loop) | Repeats one hunt cycle on a fixed interval |

Both services:

- Run as user **`vinnieb58`**
- Use working directory **`/home/vinnieb58/projects/vulture`**
- Load environment from **`/home/vinnieb58/projects/vulture/.env`**
- **`Restart=on-failure`**
- Start automatically after reboot when **enabled**

Reference unit files live in `deploy/systemd/`.

## tmux is deprecated for normal runtime

tmux was previously used to keep `discord_bot.py` and a `main.py` loop alive. That model is **deprecated** for production.

Use tmux only for **optional manual debugging** (for example, attaching to a one-off repro session). Do not start bot/scheduler in tmux on Raven for normal operation.

## Deploy / update

From the Raven repo root:

```bash
cd /home/vinnieb58/projects/vulture
bash scripts/update_raven.sh
```

Non-interactive deploy:

```bash
APP_DIR=/home/vinnieb58/projects/vulture BRANCH=main bash scripts/update_raven.sh
```

`scripts/update_raven.sh` performs, in order:

1. `git fetch` / checkout / fast-forward pull
2. Dependency install (`pip install -r requirements.txt`)
3. Python compile check
4. `pytest` (when `.venv/bin/pytest` exists)
5. `scripts/validate_step1.py`
6. One live `main.py` hunt cycle
7. Optional `scripts/smoke_multi_source.py` when present
8. **Only after all checks pass:** restart systemd services

If any step fails, services are **not** restarted (fail-safe).

Skip restarts for dry runs:

```bash
SKIP_SYSTEMD_RESTART=1 bash scripts/update_raven.sh
```

## systemd install (one-time host setup)

Copy unit files and enable services (requires sudo on Raven):

```bash
sudo cp deploy/systemd/vulture-bot.service /etc/systemd/system/
sudo cp deploy/systemd/vulture-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable vulture-bot.service vulture-scheduler.service
sudo systemctl start vulture-bot.service vulture-scheduler.service
```

Adjust `User`, paths, or `SCHEDULER_INTERVAL_SECONDS` in the unit files before copying if the host layout differs.

## Verify on Raven

```bash
systemctl is-active vulture-bot
systemctl is-active vulture-scheduler

systemctl status vulture-bot --no-pager -l
systemctl status vulture-scheduler --no-pager -l

journalctl -u vulture-bot -n 100 --no-pager
journalctl -u vulture-scheduler -n 100 --no-pager
```

Process fallback checks (useful when systemd state is ambiguous):

```bash
pgrep -af discord_bot.py
pgrep -af main.py
```

## Crow health checks

Discord slash commands report the same production signals:

- `/check_services` — `systemctl is-active` for both units, `pgrep` fallbacks, recent `journalctl` excerpts
- `/check_vulture` — DB/logs health plus combined scheduler visibility

Crow v0.1 remains read-only (no restarts from Discord).

## Dashboard Docker (unchanged)

The read-only dashboard container (`docker-compose.dashboard.yml`) is separate from bot/scheduler runtime. This systemd migration does not change dashboard Docker behavior.

## Reboot survival

Bot and scheduler survive reboot only when their units are **enabled**:

```bash
systemctl is-enabled vulture-bot vulture-scheduler
```

If either reports `disabled`, re-run `sudo systemctl enable …` as shown above.
