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
- **Control-plane domain and HTTPS.** The topbar **Settings** gear opens
  platform settings. Admins see the control-plane domain card: enter a domain
  and the platform writes the nginx site, obtains a Let's Encrypt certificate,
  and rebinds uvicorn to `127.0.0.1` so the raw IP:8000 endpoint is no longer
  exposed. Removing the domain restores `0.0.0.0` and IP access.
- **Two-factor authentication.** Every user can enable TOTP 2FA from the
  topbar **Settings** page with any authenticator app (QR code or manual
  secret). Sign-in asks for the 6-digit code after the password and also
  accepts one of ten single-use recovery codes. Recovery codes are stored
  hashed and shown once; lost authenticators can be reset by an admin with
  `manage reset-2fa --email`.
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
- **Backups and imports** — create a `pg_dump` with or without the filestore
  tarball, then download/restore. Admins can also upload a PostgreSQL `.dump`,
  `.sql`, `.sql.gz` or an Odoo `.zip` backup to replace a branch database (zip
  also restores the filestore). Database imports can optionally neutralize the
  restored database so production copies do not send email or run crons and
  integrations (Odoo 16+ uses Odoo's native neutralizer; Odoo 15 uses a core
  platform fallback). Restores run in the background worker with a live log in
  the Backups tab. Retention: keep everything for 3 days, then 7 daily, 4
  weekly, 3 monthly snapshots.
- **Fleet dashboard** — project search, grid/list layouts, equal summary
  cards, branch/running counts and a global recent-activity feed.
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

Ubuntu 22.04/24.04, ≥ 4 GB RAM (8 GB recommended), ≥ 60 GB SSD
(all six base images use roughly 20 GB):

```bash
scp -r odoo-paas root@SERVER:/root/
ssh root@SERVER
cd /root/odoo-paas
sudo ./install.sh --public-host SERVER_IP \
     --admin-email you@example.com --admin-password 'change-me'
```

The installer builds all supported Odoo base images by default (15.0–20.0),
so the first install takes longer and uses roughly 20 GB for images. Limit
that with `--odoo-versions "17.0 20.0"` if needed.

Then open `http://SERVER_IP:8000`. To stop using IP:8000, sign in as an admin,
open the topbar **Settings** gear, and add a control-plane domain. nginx and
Let's Encrypt are configured automatically; uvicorn then listens only on
`127.0.0.1`. To remove the platform later, use `sudo ./uninstall.sh`
(keeps data) or `sudo ./uninstall.sh --purge` (full wipe); preview with
`--dry-run`. Full details: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Updating

Run this from the Git checkout that contains `install.sh` (typically
`/root/odoo-paas`, not the generated `/opt/odoo-paas` copy; if the server has
no checkout, copy/scp the updated repository there instead):

```bash
cd /root/odoo-paas
git pull --ff-only
sudo ./install.sh
```

`install.sh` is the upgrader: it preserves `.env`/data, syncs the new code to
`/opt/odoo-paas`, installs dependencies, checks all Odoo base images, refreshes
the systemd unit and restarts the control plane. Existing branch containers
are recreated only on **Deploy**/**Start** (or **Update Odoo + Deploy**), so do
that when an update changes Odoo container behavior. If a release changes the
nginx templates, re-save the affected control-plane/branch domains once.

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
6. **Account security** — topbar **Settings** → *Two-factor authentication*:
   scan the QR code, verify a code, and store the recovery codes somewhere
   safe. Sign-in then requires the authenticator code after your password.

## Repository layout

```
app/
  main.py        FastAPI app factory (routes, static, lifespan)
  web.py         server-rendered pages (Jinja2 + HTMX)
  api.py         webhook, JSON API, websockets (logs + shell)
  worker.py      background build worker + retention timer
  manage.py      CLI: init, create-admin, set-password, reset-2fa, backup-all, retention, doctor
  config.py      all settings (env/.env; no os.environ elsewhere)
  odoo_config.py Odoo server options exposed in project settings
  db.py models.py auth.py twofa.py ports.py errors.py
  services/      docker_ctl (all Docker), gitops, builder, backups, monitor
  templates/ static/
docker/odoo-base/Dockerfile   shared Odoo image (odoo-paas/base:<version>)
systemd/odoo-paas.service     production unit
install.sh                    idempotent server installer
uninstall.sh                  safe uninstaller (keeps data unless --purge)
docs/                         ARCHITECTURE, DEPLOYMENT, RUNBOOK, API, SECURITY
AGENTS.md                     guide for AI agents working on this repo
LICENSE                       Apache License 2.0
NOTICE                        copyright and Odoo trademark notice
CONTRIBUTING.md               contribution and DCO guidelines
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
| [CONTRIBUTING.md](CONTRIBUTING.md) | contribution workflow, DCO and licensing |

## License

The Odoo PaaS platform code in this repository is licensed under the
[Apache License, Version 2.0](LICENSE). Redistributions must preserve the
[NOTICE](NOTICE) file and the copyright attribution. Forks are welcome, but
they must not imply that they are official Odoo products.

This license covers the platform code only. Odoo Community is licensed
separately under LGPL-3.0 and is downloaded when base images are built; Odoo
Enterprise addons are proprietary and require a valid Odoo Enterprise
subscription. The Odoo name and logo are trademarks of Odoo S.A. This project
is independent and is not affiliated with or endorsed by Odoo S.A.

Contributions are accepted under Apache-2.0 with a Developer Certificate of
Origin sign-off; see [CONTRIBUTING.md](CONTRIBUTING.md).

## Non-goals (vs. Odoo.sh)

Single server, single worker, no per-branch custom Odoo versions side by side,
no staging database restoration workflow (staging starts empty — create a
backup on production and restore it via the Backups tab if you want the data),
no TLS on raw branch ports (custom branch domains are supported), no
autoscaling. See ARCHITECTURE.md for the reasoning.
