# Aviary Project Context

_Authoritative transition document — last refreshed: 2026-06-10 (UTC)_

This repository is the **Aviary monorepo**: one codebase that hosts multiple services for a personal/self-hosted platform. **Vulture** is one service inside it, not the whole platform.

For Vulture-specific architecture detail, see `docs/current/VULTURE_2_0_ARCHITECTURE.md`. For Raven deploy/runtime, see `docs/current/RAVEN_SYSTEMD_RUNTIME.md`.

---

## 1. Aviary vision

**Aviary** is the umbrella platform for a home-lab operations stack:

- A single physical host (**Raven**) runs deal-hunting, Discord ops, monitoring, storage, and future services.
- Services share deployment scripts, observability patterns, and (where practical) Python modules — but each service has a clear boundary and name.
- The goal is **observe-first, control-later**: read-only health surfaces (Crow, Canary, dashboard) before mutating production from chat or UI.

Naming follows bird motifs. Services may start in this repo and later split into their own containers or repositories when boundaries harden.

---

## 2. Service catalog

| Service | Role | Status in repo | Primary paths |
|---------|------|----------------|---------------|
| **Aviary** | Umbrella platform / naming | Conceptual | `docs/current/AVIARY_PROJECT_CONTEXT.md` (this file) |
| **Raven** | Physical headless Ubuntu server + infrastructure host | Production target | Host paths under `/home/vinnieb58/projects/vulture`; systemd units in `deploy/systemd/` |
| **Vulture** | Marketplace deal-hunting engine | **Active** — core product | `main.py`, `engine/`, `adapters/`, `discord_bot.py` (hunt commands) |
| **Crow** | Discord operations / read-only Raven+Vulture checks | **Active v0.2** | `crow/`, `docs/CROW_V0_1.md`, `docs/CROW_V0_2.md` |
| **Canary** | Periodic read-only Raven monitoring (JSON + logs) | **Active v0.1** | `canary/`, `docker-compose.canary.yml` |
| **Dashboard** | Read-only web ops UI (Raven health, Vulture runtime, storage) | **Active v0.2** | `dashboard/`, `docker-compose.dashboard.yml` |
| **Roost** | Storage / NAS layer (mounts under `/mnt/storage`) | **Operational** (filesystem + monitoring) | Observed by dashboard, Canary, Crow; not a separate daemon yet |
| **Nest** | Future unified dashboard / API surface | Planned | Referenced in Crow docs as future consumer of `crow/system/` |
| **Magpie** | Planned | Emerging / unnamed scope | — |
| **Owl** | Planned | Emerging / unnamed scope | — |
| **Pelican** | Planned (backup volume name exists) | Storage label `pelican_backup` on Raven | `/mnt/storage/pelican_backup` |
| **Corvus** | Planned | Emerging / unnamed scope | — |

**Crow** and **Vulture hunt commands** share `discord_bot.py` and `vulture-bot.service` today. That is a deployment convenience, not a claim that Crow *is* Vulture.

---

## 3. Raven hardware / software inventory

_Verified from repo config and docs; live hardware may drift — confirm on host._

### Host

| Item | Value (from repo/docs) |
|------|-------------------------|
| Hostname | `raven` |
| OS role | Headless Ubuntu server |
| Deploy user | `vinnieb58` |
| App root | `/home/vinnieb58/projects/vulture` |
| Python venv | `/home/vinnieb58/projects/vulture/.venv` |
| LAN example | `192.168.1.143` (dashboard README) |
| Tailscale example | `100.82.1.18` (dashboard README) |

### Software stack (production)

| Component | Mechanism |
|-----------|-----------|
| Vulture bot + Crow | `vulture-bot.service` → `discord_bot.py` |
| Vulture hunt cycles | `vulture-scheduler.timer` → `vulture-scheduler.service` (oneshot `main.py`) |
| Dashboard | Docker `vulture-dashboard` on port **8088** (`docker-compose.dashboard.yml`) |
| Canary | Docker compose (`docker-compose.canary.yml`), host network |
| Docker | Used for dashboard + Canary; Vulture bot/scheduler are **not** containerized |
| Samba, Tailscale, SSH, Docker, Portainer | Checked by Crow/Canary/dashboard |

### Deprecated for normal production

- **tmux** for bot/scheduler longevity — replaced by **systemd** (see `docs/current/RAVEN_SYSTEMD_RUNTIME.md`).

---

## 4. Current storage architecture (Roost)

Active Raven storage uses the **`/mnt/storage/`** parent with per-volume subpaths. Dashboard, Canary, and deploy scripts align on this layout.

| Label | Mount path | Notes |
|-------|------------|--------|
| Root SSD | `/` | Required |
| MicroSD | `/mnt/storage/microsd` | Required in dashboard config |
| Toshiba EXT | `/mnt/storage/toshiba_ext` | NTFS external |
| Pelican Backup | `/mnt/storage/pelican_backup` | Optional backup volume |
| Raven NVME | `/mnt/storage/raven_nvme` | Optional |
| Roost Spinning 0 | `/mnt/storage/roost_spinning_0` | Roost-named spinning disk |
| portable_beast | `/mnt/storage/portable_beast` | **Legacy** — still checked for migration visibility |

**Legacy paths** (`/mnt/microsd`, `/mnt/portable_beast`, `/mnt/toshiba_ext`) appear in **Crow default config** (`crow/config.py`) unless overridden via `CROW_EXPECTED_MOUNTS`. Dashboard and Canary use `/mnt/storage/*`. Set `CROW_EXPECTED_MOUNTS` on Raven for consistent `/check storage` output.

Deploy scripts create stable parent directories even when drives are unplugged:

```bash
sudo mkdir -p /mnt/storage/{microsd,toshiba_ext,portable_beast,pelican_backup,raven_nvme,roost_spinning_0}
```

---

## 5. Network / access model

| Access | Purpose |
|--------|---------|
| SSH (22) | Admin shell on Raven |
| LAN | Home network (e.g. `192.168.1.143`) |
| Tailscale | Remote access (e.g. `100.82.1.18`) |
| Dashboard `:8088` | Read-only ops UI — **no auth**; LAN/Tailscale only |
| Discord | Vulture hunt control + Crow `/check` commands |
| Samba (445) | File sharing (Roost-related) |
| Portainer (9443) | Container management (observed, not controlled by Crow v0.2) |

Secrets (`DISCORD_BOT_TOKEN`, `DISCORD_WEBHOOK_URL`, etc.) live in **`.env` on Raven only** — never committed.

---

## 6. Runtime / deployment model

```text
Aviary on Raven
├── systemd (native Python)
│   ├── vulture-bot.service      → discord_bot.py (Vulture hunts + Crow)
│   └── vulture-scheduler.timer  → vulture-scheduler.service → main.py (oneshot)
├── Docker (read-only observability)
│   ├── vulture-dashboard        → :8088
│   └── canary                   → data/canary_status.json, logs/canary.log
└── SQLite + logs (Vulture data plane)
    ├── data/vulture.db
    └── logs/vulture.log
```

### Deploy scripts

| Script | Use |
|--------|-----|
| `scripts/update_raven_quick.sh` | Default operational pull — deps, compile, systemd, Docker rebuild |
| `scripts/update_raven.sh` | Full validation + one live hunt cycle before service restart |
| `scripts/rebuild_docker.sh` | Docker-only stack rebuild |

### Hunt source (Vulture)

| `VULTURE_HUNT_SOURCE` | Behavior |
|-----------------------|----------|
| `db` | **Production default** (`.env.example`, Raven `.env`) |
| `yaml` | Legacy/dev — `config/hunts.yaml` only |
| `mixed` | Both; YAML wins on name collision |

Code fallback in `main.py` is `yaml` if the variable is unset — production must set `db` in `.env`.

---

## 7. Current Vulture status

Vulture 2.0+ is **production-active** on Raven:

- Discord slash commands create/manage hunts in SQLite (`/hunt`, `/hunt_list`, …).
- `main.py` runs hunt cycles via adapter registry dispatch, multi-source fan-out, deterministic `engine/rules.py` filtering, link dedupe, Discord webhook alerts.
- Intent translation routes vehicles through `engine/intent_translator_v2.py`; other verticals use deterministic builders.
- Vertical-aware source selection in `engine/source_selection.py` fans out to registered adapters per category.

### Registered runtime adapters (`adapters/registry.py`)

| Source | Registry `status` | Summary |
|--------|-------------------|---------|
| `craigslist` | stable | Primary marketplace adapter |
| `mercari` | beta | Requests/GraphQL |
| `microcenter` | beta | Playwright; computer/laptop verticals |
| `offerup` | experimental | GeoIP-only location |
| `carsdotcom` | experimental | Playwright; vehicles; flaky |
| `swappa` | experimental | Requests/HTML |
| `bestbuy` | experimental | Playwright |
| `newegg` | experimental | Requests/HTML |

Probe-only / no runtime adapter: eBay and others under `experiments/adapters/`.

---

## 8. Crow / Canary / Roost / dashboard status

### Crow (v0.2.0)

- Read-only Discord ops: `/check raven`, `/check services`, `/check storage`, `/check docker`, `/check reboot`, `/check logs`, … plus v0.1 commands (`/raven_status`, `/check_vulture`, …).
- Business logic in `crow/system/` for reuse by Canary and future Nest.
- **No** restart/stop/reboot in v0.2.

### Canary (v0.1)

- Dockerized periodic health checks → `data/canary_status.json`, `logs/canary.log`.
- Covers internet, network, services, storage (`/mnt/storage/*`), Docker, Vulture runtime, failed systemd units.
- **No** Discord alerting yet (JSON is alert-ready).

### Dashboard (v0.2)

- Read-only Flask UI at `:8088` — Raven health, Vulture hunts/adapters, Storage/Roost, Docker, logs.
- Uses host `systemctl` via chroot/nsenter; resilient to unplugged optional drives.

### Roost

- Not a separate process — the **storage/NAS layer** on Raven (`/mnt/storage`, Samba, automount units).
- Monitored by dashboard (Storage/Roost section), Canary, and Crow `/check storage`.

---

## 9. Planned services and roadmap

| Priority | Item |
|----------|------|
| Near-term | Adapter smoke evidence; docs/code sync; Crow mount defaults → `/mnt/storage` |
| Medium | Canary → Discord alerts; Nest consuming `crow/system/` APIs |
| Medium | Controlled restarts (Crow v0.3+) with admin gating |
| Longer | Extract Crow to own container/repo if boundaries require |
| Emerging | Magpie, Owl, Corvus — scope TBD |

Vulture roadmap detail: `docs/current/VULTURE_2_0_ROADMAP.md`.

---

## 10. Known constraints

1. **Single-operator / self-hosted** — not multi-tenant SaaS.
2. **Deterministic runtime** — no LLM in listing accept/reject path.
3. **Adapter honesty** — experimental adapters may return `[]` on blocks; hunt cycle continues.
4. **OfferUp** — GeoIP location only; city arg is advisory.
5. **Cars.com / Playwright adapters** — need Chromium on Raven; Cloudflare/Akamai sensitivity.
6. **Crow default mounts** — legacy paths unless `CROW_EXPECTED_MOUNTS` is set (see §4).
7. **`main.py` default hunt source** — `yaml` if env unset; Raven `.env` must use `db`.
8. **Dashboard** — no authentication; trusted network only.
9. **Canary blind spot** — cannot report failures that prevent boot entirely.

---

## 11. Guidance for future Cursor / AI agents

1. **Read this file first** for platform vs service boundaries.
2. **Vulture docs** describe the deal-hunting service only — do not treat them as whole-platform docs.
3. **Source of truth order**: code → `docs/current/CODEBASE_STATUS.md` → `docs/current/AVIARY_PROJECT_CONTEXT.md` → archived logs.
4. **Do not** assume YAML hunts are production default — check `VULTURE_HUNT_SOURCE` and `.env.example`.
5. **Adapter registry exists** — `adapters/registry.py`; do not describe it as future work.
6. **Scheduler** — systemd timer + oneshot service, not tmux loop.
7. **Storage** — prefer `/mnt/storage/*` in new docs and config examples.
8. **Scope discipline** — documentation tasks should not change adapter behavior, Discord commands, or production `.env`.
9. **Commit boundary** — docs-only unless a one-line comment/docstring fixes a clear code/doc contradiction.

---

## 12. Documentation Accuracy Notes

_Last verification: 2026-06-10_

### Verified from code

- Adapter registry with 8 registered sources (`adapters/registry.py`).
- `main.py` uses `get_adapter()`, multi-source `_expand_hunt_sources()`, hunt modes `yaml|db|mixed`.
- Crow `__version__ = "0.2.0"`; `crow/system/` health modules exist.
- Canary app, checks, storage paths under `/mnt/storage/*` (`canary/config.py`).
- Dashboard `storage_config.py` expected drives and v0.2 features (`dashboard/README.md`).
- systemd units: `vulture-bot.service`, `vulture-scheduler.service` (oneshot), `vulture-scheduler.timer`.
- `.env.example` sets `VULTURE_HUNT_SOURCE=db`.
- Vertical source profiles in `engine/source_selection.py`.

### Inferred from docs / history

- Raven hostname, IP examples, deploy user/path from `RAVEN_SYSTEMD_RUNTIME.md` and dashboard README.
- Hardware inventory detail (CPU, RAM, disk models) — **not** fully enumerated in repo; treat host as generic headless Ubuntu.
- Nest/Magpie/Owl/Corvus scopes — naming only, no implementation.
- Title-intelligence refinement workstream (`feature/title-intelligence-v1`) from March 2026 status logs.

### Remains unverified (confirm on live Raven)

- Current git branch/commit on production host.
- Whether `CROW_EXPECTED_MOUNTS` is set to `/mnt/storage/*` in production `.env`.
- Live adapter reliability per source in current hunt cycles.
- Physical drive attach state (optional volumes missing vs present).
- Discord webhook/bot token configuration.

### Code / doc contradictions discovered (documented, not code-changed)

| Topic | Code / config | Stale doc or default |
|-------|---------------|----------------------|
| Crow storage defaults | `crow/config.py` → `/mnt/microsd`, `/mnt/portable_beast` | Dashboard/Canary use `/mnt/storage/*` |
| Hunt source default | `main.py` defaults to `yaml` if unset | Production `.env.example` and ops docs say `db` |
| README | Entire file described v1.0 YAML-primary workflow | Superseded by this transition |
