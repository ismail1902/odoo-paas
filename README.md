# Odoo PaaS

A self-hosted, small-scale clone of [Odoo.sh](https://www.odoo.sh): push to a
Git branch, get a running Odoo with its own database, port and logs.

Built for one dedicated Ubuntu server, IP:port based (no domain or TLS
termination required), operated through a small web UI plus a JSON API.

```
Browser ──► control plane (FastAPI + HTMX) ──► Docker
                │  http://SERVER:8000           ├─ odoo-paas-pg*       (shared PostgreSQL 15–18 + pgvector)
                │                               ├─ odoo-paas-mailpit  (catches all outgoing mail)
                └─ SQLite state, build worker   └─ odoo-paas-branch-N  (one per branch)
                                                   http://SERVER:9000+ ─► Odoo
```

## Features

- **Projects → branches → builds.** A project wraps a Git repository; every
  branch you add gets a stage (production / staging / development), a host
  port from a pool (9000–9990) and its own PostgreSQL database.
- **Per-project Odoo and PostgreSQL versions.** Pick the Odoo version
  (any version with a built base image, e.g. 15.0–20.0) and a compatible
  PostgreSQL/pgvector version when creating a project; every branch copies
  both choices. The dropdown only offers valid combinations
  (PostgreSQL 15–18 for Odoo 15–19, PostgreSQL 16–18 for Odoo 20).
- **AI-ready for Odoo 19/20.** The shared PostgreSQL 15–18 images include
  [pgvector](https://github.com/pgvector/pgvector); the `vector` extension is
  enabled in every branch database (and in `template1`, so new databases
  inherit it) on the project's chosen PostgreSQL container.
- **Optional Enterprise addons.** Add an Enterprise addons repository URL
  per project; it is cloned at the matching Odoo version branch and added
  to the Odoo addons path.
- **Update Odoo core.** *Update Odoo + Deploy* rebuilds
  `odoo-paas/base:<version>` from the latest Odoo source for the selected
  version, runs `-u all` when the database exists, and redeploys the branch
  in one serialized build.
- **Control-plane domain and HTTPS.** An admin-only gear button in the topbar
  opens platform settings. Enter a domain and the platform writes the nginx
  site, obtains a Let's Encrypt certificate, and rebinds uvicorn to
  `127.0.0.1` so the raw IP:8000 endpoint is no longer exposed. Removing the
  domain restores `0.0.0.0` and IP access.
- **Custom domains and HTTPS for branches.** With Proxy mode enabled, each
  branch can have a domain. nginx is configured automatically (including the
  websocket/gevent upstream) and Let's Encrypt certificates are issued and
  renewed with certbot.
- **Push-to-deploy.** GitHub webhooks (HMAC-verified) trigger builds; a manual
  *Deploy* button does the same.
- **Works before your first push.** An empty repository (or a branch that is
  not pushed yet) still deploys: Odoo starts with the base modules and the
  modules you configured install automatically on the first push.
- **No repository needed to start.** Create a project without a Git URL and
  add it later in *Project settings*; until then branches deploy base Odoo.
- **Build pipeline** — clone/checkout → per-branch `pip install -r
  requirements.txt` → database init (`-i`) or update (`-u all`) → start the
  Odoo container → HTTP healthcheck. Live log in the UI.
- **Project settings page** — gear button on each project: repository URL,
  private-repo credentials, and Odoo server configuration (workers, cron
  threads, limits, logging, SMTP, proxy/security flags, developer mode, plus
  an advanced field for any other Odoo CLI option). Each option can be
  enabled or disabled; changes apply on the next Deploy/Start.
- **Branch tabs** — History (builds + commits + activity), Logs (live
  container/build log stream), Shell (interactive web terminal via Docker
  exec), Backups (create/download/restore, Odoo.sh-style retention), Monitor
  (live CPU/RAM/network/disk charts, disk gauge and generated insights),
  Settings.
- **Backups** — `pg_dump` + filestore tarball. Retention: keep everything for
  3 days, then 7 daily, 4 weekly, 3 monthly snapshots.
- **Fleet dashboard** — project cards with branch/running counts, available
  Odoo versions and a global recent-activity feed.
- **Light, dark or system theme** — persistent per browser, switchable from
  the topbar; terminals and monitor charts follow the selected theme.
- **Roles** — `developer` < `tester` < `admin`. Shell access is stage-aware:
  development = any role, staging = tester+, production = admin only.
- **Mailpit** — every outgoing email from every branch is captured by
  Mailpit instead of being sent (staging/dev safety, like Odoo.sh).
- **No domain needed.** Everything is reached over `http://SERVER_IP:PORT`.

## Quickstart (local UI development)

Docker is simulated on macOS (`MOCK_DOCKER` defaults to `true` on darwin):

```bash
cd odoo-paas
python3.13 -m venv .venv          # any Python >= 3.10
.venv/bin/pip install -r requirements.txt
cp .env.example .env              # edit ADMIN_EMAIL / ADMIN_PASSWORD
MOCK_DOCKER=1 .venv/bin/uvicorn app.main:app --reload
# open http://127.0.0.1:8000 and log in with the seeded admin
```

## Server deployment

Ubuntu 22.04/24.04, ≥ 4 GB RAM (8 GB recommended), ≥ 40 GB SSD:

```bash
scp -r odoo-paas root@SERVER:/root/
ssh root@SERVER
cd /root/odoo-paas
sudo ./install.sh --public-host SERVER_IP \
     --admin-email you@example.com --admin-password 'change-me'
```

Then open `http://SERVER_IP:8000`. To stop using IP:8000, sign in as an admin,
open the topbar **Settings** gear, and add a control-plane domain. nginx and
Let's Encrypt are configured automatically; uvicorn then listens only on
`127.0.0.1`. Full details: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Using it

1. **New project** — dashboard → *New project*: name + Odoo version, and a
   Git URL if you have one (the URL is optional and can be added later in
   *Project settings*). Add the shown webhook URL + secret in GitHub →
   Settings → Webhooks (content type `application/json`).
2. **New branch** — project board → *New branch*: git branch name + stage.
   The first build starts automatically and the Odoo URL appears
   (`http://SERVER_IP:9xxx`).
3. **Push** to the branch → webhook → build → running.
4. **Project settings** — the gear button on the project page edits the
   repository URL/private-repo credentials and the Odoo server configuration.
5. Per-branch *Settings* tab: modules to install on first DB init, run tests
   (development), `-u all` on every deploy.

## Repository layout

```
app/
  main.py        FastAPI app factory (routes, static, lifespan)
  web.py         server-rendered pages (Jinja2 + HTMX)
  api.py         webhook, JSON API, websockets (logs + shell)
  worker.py      background build worker + retention timer
  manage.py      CLI: init, create-admin, set-password, backup-all, retention, doctor
  config.py      all settings (env/.env; no os.environ elsewhere)
  odoo_config.py Odoo server options exposed in project settings
  db.py models.py auth.py ports.py errors.py
  services/      docker_ctl (all Docker), gitops, builder, backups, monitor
  templates/ static/
docker/odoo-base/Dockerfile   shared Odoo image (odoo-paas/base:<version>)
systemd/odoo-paas.service     production unit
install.sh                    idempotent server installer
docs/                         ARCHITECTURE, DEPLOYMENT, RUNBOOK, API, SECURITY
AGENTS.md                     guide for AI agents working on this repo
```

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | topology, data model, build pipeline, design decisions |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | server install, webhooks, updating the platform |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | day-2 operations and troubleshooting |
| [docs/API.md](docs/API.md) | HTTP/WebSocket endpoints and payloads |
| [docs/SECURITY.md](docs/SECURITY.md) | roles, secrets, exposure, hardening |
| [AGENTS.md](AGENTS.md) | conventions and invariants for AI agents |

## Non-goals (vs. Odoo.sh)

Single server, single worker, no per-branch custom Odoo versions side by side,
no staging database restoration workflow (staging starts empty — create a
backup on production and restore it via the Backups tab if you want the data),
no TLS/domains, no autoscaling. See ARCHITECTURE.md for the reasoning.
