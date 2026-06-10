# Raven production runtime (systemd)

**Raven** is the Aviary physical host. This document covers how **Vulture** (bot + hunt scheduler) and sibling Docker services run on Raven. Platform overview: [AVIARY_PROJECT_CONTEXT.md](AVIARY_PROJECT_CONTEXT.md).

Raven runs the Vulture Discord bot (Vulture hunts + Crow) and hunt scheduler as **systemd services**, not tmux sessions.

## Services

| Unit | Process | Purpose |
|------|---------|---------|
| `vulture-bot.service` | `discord_bot.py` | Discord control plane (Crow + Vulture hunt commands) |
| `vulture-scheduler.service` | `main.py` (oneshot) | Runs one hunt cycle and exits |
| `vulture-scheduler.timer` | — | Schedules hunt cycles every 15 minutes |

### Scheduler architecture (oneshot + timer)

- **`vulture-scheduler.service`** is `Type=oneshot`. It runs one `main.py` hunt cycle and exits with status 0 on success.
- **`vulture-scheduler.timer`** is the actual scheduler heartbeat. It triggers the oneshot service shortly after boot, then every 15 minutes after the previous run finishes (`OnUnitInactiveSec=15min`).
- Between timer runs, **`vulture-scheduler.service` is expected to be `inactive`/`dead`**. That is normal — it does not mean the scheduler failed.

Both long-running and oneshot units:

- Run as user **`vinnieb58`**
- Use working directory **`/home/vinnieb58/projects/vulture`**
- Load environment from **`/home/vinnieb58/projects/vulture/.env`**

Reference unit files live in `deploy/systemd/`.

## tmux is deprecated for normal runtime

tmux was previously used to keep `discord_bot.py` and a `main.py` loop alive. That model is **deprecated** for production.

Use tmux only for **optional manual debugging** (for example, attaching to a one-off repro session). Do not start bot/scheduler in tmux on Raven for normal operation.

## Deploy / update

From the Raven repo root:

```bash
cd /home/vinnieb58/projects/vulture
```

### Quick deploy (default for operational fixes)

Use this for dashboard, systemd, docs, or service changes when you do **not** need an immediate full hunt cycle:

```bash
./scripts/update_raven_quick.sh
```

Quick deploy but intentionally run one scheduler cycle after update:

```bash
./scripts/update_raven_quick.sh --run-once
```

`scripts/update_raven_quick.sh` performs, in order:

1. `git fetch` / fast-forward pull on the **current** branch
2. Dependency install (`pip install -r requirements.txt`) when present
3. Python compile check (syntax only)
4. Install/update systemd units from `deploy/systemd/`
5. Restart `vulture-bot.service` and `vulture-scheduler.timer` (when present)
6. Rebuild/restart Docker compose stacks via `scripts/rebuild_docker.sh`
7. Print final status (git, bot, timer, scheduler worker, dashboard)

It does **not** run `validate_step1.py`, `main.py`, adapter validation, or destructive cleanup.

Optional flags:

```bash
./scripts/update_raven_quick.sh --no-docker     # skip Docker stack rebuild/restart
./scripts/update_raven_quick.sh --no-services # skip systemd install/restarts
./scripts/update_raven_quick.sh --help
```

`vulture-scheduler.service` is a one-shot worker when using the timer model. It stays inactive between timer triggers; that is normal. The quick script only starts it when you pass `--run-once`.

### Docker-only rebuild

Use this when you only need to rebuild/restart compose stacks (no git pull, no systemd, no hunt):

```bash
./scripts/rebuild_docker.sh
```

Rebuild one stack:

```bash
./scripts/rebuild_docker.sh --file docker-compose.dashboard.yml
```

Restart without rebuilding images:

```bash
./scripts/rebuild_docker.sh --no-build
```

By default, `rebuild_docker.sh` rebuilds every `docker-compose*.yml` file in the repo root. Add new stacks by dropping in another compose file; optional HTTP health probes are configured in the script's `STACK_HEALTH_URLS` map.

Pair with full deploy when dashboard (or other container) code changed but you already ran the heavy validation path:

```bash
bash scripts/update_raven.sh
./scripts/rebuild_docker.sh
```

### Full deploy (validation + immediate hunt cycle)

Use this when you want full validation and one live hunt cycle before services restart:

```bash
bash scripts/update_raven.sh
```

Non-interactive full deploy:

```bash
APP_DIR=/home/vinnieb58/projects/vulture BRANCH=main bash scripts/update_raven.sh
```

`scripts/update_raven.sh` performs, in order:

1. `git fetch` / checkout / fast-forward pull
2. Dependency install (`pip install -r requirements.txt`)
3. Python compile check
4. `scripts/validate_step1.py` (data layer validation)
5. One live `main.py` hunt cycle
6. **Only after all checks pass:** install systemd units, `daemon-reload`, enable services, restart

The update script copies all unit files from `deploy/systemd/` into `/etc/systemd/system/`, enables `vulture-bot.service` and `vulture-scheduler.timer`, and restarts both. It does **not** enable `vulture-scheduler.service` as a long-running daemon.

If any step fails, services are **not** restarted (fail-safe).

Skip restarts for dry runs:

```bash
SKIP_SYSTEMD_RESTART=1 bash scripts/update_raven.sh
```

## systemd install (one-time host setup)

Normally `scripts/update_raven.sh` installs units on every deploy. For manual one-time setup:

```bash
sudo cp deploy/systemd/vulture-bot.service /etc/systemd/system/
sudo cp deploy/systemd/vulture-scheduler.service /etc/systemd/system/
sudo cp deploy/systemd/vulture-scheduler.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable vulture-bot.service vulture-scheduler.timer
sudo systemctl start vulture-bot.service vulture-scheduler.timer
```

Do **not** enable `vulture-scheduler.service` directly — the timer triggers it.

Adjust `User`, paths, or timer intervals in the unit files before copying if the host layout differs.

## Verify on Raven

```bash
systemctl status vulture-scheduler.timer --no-pager -l
systemctl status vulture-scheduler.service --no-pager -l
systemctl list-timers --all | grep vulture
journalctl -u vulture-scheduler.service -n 80 --no-pager

systemctl is-active vulture-bot
systemctl is-active vulture-scheduler.timer
systemctl is-enabled vulture-scheduler.timer

systemctl status vulture-bot --no-pager -l
journalctl -u vulture-bot -n 100 --no-pager
```

Expected healthy signals:

- `vulture-scheduler.timer` is **active** and **enabled**
- `systemctl list-timers --all` shows `vulture-scheduler.timer` with a next run time
- `vulture-scheduler.service` may be **inactive/dead** between runs after `status=0/SUCCESS`
- Journal shows recent `Hunt cycle completed` lines

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

The read-only dashboard container (`docker-compose.dashboard.yml`) is separate from bot/scheduler runtime. The dashboard treats **`vulture-scheduler.timer`** as the scheduler heartbeat. An inactive oneshot service between runs is healthy when the timer is active and recent hunt-cycle logs exist.

## Reboot survival

Bot and scheduler survive reboot only when their units are **enabled**:

```bash
systemctl is-enabled vulture-bot vulture-scheduler.timer
```

If either reports `disabled`, re-run `sudo systemctl enable …` as shown above.

### Health check scripts (Priority 6)

Repo-tracked scripts produce a full read-only health report. Vulture checks use **systemd**, not tmux (tmux is listed only as optional debug output).

Install on Raven:

```bash
cd /home/vinnieb58/projects/vulture
cp scripts/raven_healthcheck.sh ~/raven_healthcheck.sh
chmod +x ~/raven_healthcheck.sh
```

Run after reboot or anytime:

```bash
~/raven_healthcheck.sh
~/raven_healthcheck.sh --post-reboot
```

See `docs/current/RAVEN_RESTART_SURVIVAL_PLAN.md` and `docs/current/RAVEN_BOOT_WARNINGS.md` (optional Priority 7 noise reduction).
