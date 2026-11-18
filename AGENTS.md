# AGENTS.md — working on the Odoo PaaS codebase

This file is for AI coding agents (and humans in a hurry). Read it before
changing anything; it encodes the invariants that keep the platform working.

## What this is

A self-hosted mini-Odoo.sh: one Ubuntu server runs a FastAPI+HTMX control
plane that manages Odoo branch containers (Docker), a shared PostgreSQL and
Mailpit. See `docs/ARCHITECTURE.md` for topology. `README.md` has the user
guide. `docs/RUNBOOK.md` has operations. `docs/DEPLOYMENT.md` has install.

## Golden rules

1. **All Docker access goes through `app/services/docker_ctl.py`.** Never
   call `docker`/`docker-py` elsewhere (the worker, web and API layers use it;
   CLI helper `_docker_exec_pg` exists for Postgres host-side commands).
2. **All settings go through `app/config.py`.** No `os.environ` reads
   anywhere else. New knobs: add to `Settings` + `.env.example` + (if server
   relevant) `install.sh`.
2b. **All Odoo server CLI options go through `app/odoo_config.py`.**
   Project-level config is JSON on `Project.odoo_config`; `docker_ctl.
   server_command` merges it with platform-fixed flags. Do not let users
   override `-d`, addons/data paths, HTTP interface/port, DB connection,
   init/update or test flags from the advanced field.
3. **The container user is uid/gid 1000 (`odoo`).** Any host directory that
   gets bind-mounted into a branch container must be chowned
   (`docker_ctl._make_container_writable`). Keep `CONTAINER_UID` in sync with
   `useradd --uid 1000` in `docker/odoo-base/Dockerfile`.
4. **`MOCK_DOCKER=true` must keep working.** On macOS it short-circuits all
   Docker ops; the UI must remain usable in mock mode. Every new docker_ctl
   function needs a mock path.
5. **Odoo version is per project.** `Project.odoo_version` is copied to
   `Branch.odoo_version` at creation; branch containers use
   `docker_ctl.image_for(branch)` (`odoo-paas/base:<version>`). Never use
   `settings.BASE_IMAGE` directly for branch work; the project form offers
   `docker_ctl.list_base_versions()`.
5. **Builds run one at a time** in a single worker thread
   (`app/worker.py`). Don't introduce parallel builds without redesigning
   state transitions (queued → running → succeeded/failed). Base-image
   rebuilds use the same queue via `Build.rebuild_base` and
   `docker_ctl.build_base_image`, so they are serialized too.
6. **Databases are derived from names**: `paas_<project.slug>_<branch>` via
   `web._db_name()`. Don't rename a branch's DB independently or history is
   lost. A database belongs to exactly one Odoo series — never point two
   series at the same DB. The shared container is
   `pgvector/pgvector:pg16-trixie` (or `:pg16` for bookworm volumes), and
   every branch DB must have the `vector`
   extension (Odoo 19/20 AI): `docker_ctl.ensure_database` enables it on each
   deploy, and `install.sh` seeds `template1` plus existing databases.
7. **No comments unless non-obvious.** Existing style: module docstrings +
   minimal inline comments. Match it.

## Layout

| File | Responsibility |
|---|---|
| `app/main.py` | app factory, lifespan (init_db → seed admin → worker), static mount |
| `app/web.py` | ALL HTML routes + Jinja context helpers (`_ctx`, `_back`, `_page_user`) |
| `app/api.py` | webhook, `/healthz`, `/api/monitor`, WS logs/shell |
| `app/models.py` | SQLModel tables + status constants (`BUILD_*`, `STAGES`, `ROLES`) |
| `app/odoo_config.py` | curated Odoo server options, JSON/config validation, CLI args |
| `app/auth.py` | PBKDF2 hashing, signed-cookie sessions, `has_role`, `can_open_shell` |
| `app/ports.py` | port pool allocation (persisted on Branch, OS bind test) |
| `app/worker.py` | build claim loop, stale-build recovery, hourly retention |
| `app/manage.py` | argparse CLI (init, create-admin, set-password, backup-all, retention, doctor) |
| `app/services/builder.py` | the 5-step pipeline; writes `logs/build-<id>.log` |
| `app/services/gitops.py` | git sync/checkout, commit metadata, webhook HMAC verify |
| `app/services/backups.py` | dump/restore/retention (3d + 7/4/3 buckets) |
| `app/services/monitor.py` | psutil host stats + container stats |
| `app/services/nginx.py` | per-branch nginx config, Let's Encrypt, reload/rollback |
| `app/templates/` | base/login/dashboard/project/branch/build + `partials/` (htmx) |
| `app/static/` | `css/app.css`, `js/theme.js`, `js/dashboard.js`, `js/terminal.js` (xterm.js), `js/monitor.js` (Chart.js polling) |
| `docker/odoo-base/Dockerfile` | shared Odoo image; system-wide Odoo requirements |
| `install.sh` | idempotent server installer (keep it idempotent!) |
| `systemd/odoo-paas.service` | production unit, runs as root, `${PORT}` from `.env` |

## Dev workflow (macOS/Linux)

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
MOCK_DOCKER=1 .venv/bin/uvicorn app.main:app --reload    # http://127.0.0.1:8000
.venv/bin/python -m app.manage doctor                    # diagnostics
```

There is no test suite; verify by:
- `MOCK_DOCKER=1` boot + login + create project/branch + queued build
  transitions in the UI (mock builds succeed immediately).
- `python -m compileall app` for syntax.
- On a Linux host with Docker: full build of a small public repo.

## Common change recipes

- **New branch setting**: add column to `Branch` (models.py) → form in
  `templates/branch.html` Settings tab → parse in `web.branch_settings` →
  honor it in `builder.run_build` / `docker_ctl`.
- **New model column**: add the field, then an additive migration in
  `db._migrate()` (`PRAGMA table_info` + `ALTER TABLE … ADD COLUMN`, backfill
  empties) — `create_all` never alters existing tables, so the running
  server's SQLite DB must be upgraded in place.
- **New page/partial**: route in `web.py` (use `_page_user` + `_ctx`) →
  template extending `base.html` → link it somewhere visible.
- **New pipeline step**: insert `step("...")` + work in `builder.run_build`,
  log via the `log()` closure; failures must raise (`BuildError`) so the
  standard failure path (stop container, mark failed, Event) runs.
- **New metric**: extend `monitor.branch_stats` (keep it JSON-serializable);
  both `/api/monitor` and the monitor partial consume it unchanged.

## Gotchas

- `templates.TemplateResponse` uses the current Starlette signature
  `(request, name, context)` everywhere in `web.py`; keep it consistent
  (the legacy `(name, context)` form crashes on current Starlette).
- Datetime model fields are annotated `NaiveDatetime` (pydantic) and filled
  with `models._now()` / `datetime.now(timezone.utc).replace(tzinfo=None)`.
  SQLModel's plain `datetime` mapping rejects naive values — keep the
  annotation on any new datetime field.
- SQLite + threads: always use `session_scope()` from `app/db.py`; the
  worker and request handlers run in different threads. Sessions use
  `expire_on_commit=False` on purpose: detached objects are passed to
  services after commits.
- Host-side git runs as root over checkouts owned by uid 1000: the installer
  sets `git config --global --add safe.directory '*'`; keep that line.
- The port pool is finite (991 ports); branches get ports forever, even
  stopped. Deleting a branch frees its port.
- `builds/branch-N/{repo,venv,filestore}` are the only per-branch state on
  disk besides backups; deleting a branch deletes them (`shutil.rmtree`).
- The base image installs Odoo deps system-wide with
  `pip --break-system-packages` (automatically omitted on older pip);
  branch venvs are created `--system-site-packages` and must not reinstall
  Odoo itself. Odoo 15/16 images use `python:3.9-slim-bookworm` (Python 3.9;
  PostgreSQL 10/12+), Odoo 17–19 use `debian:bookworm-slim` (Python 3.11;
  PostgreSQL 13+), and Odoo >= 20 uses `python:3.12-slim-bookworm` (Python
  >= 3.12, PostgreSQL >= 16 for `any_value()`).
- `gitops.sync()` returns `None` when the branch has no commits yet (empty
  repo / not pushed) or when the project has no repo URL: the builder then
  deploys base Odoo, clears any stale checkout, and later builds
  auto-install configured modules that are not installed yet
  (`docker_ctl.installed_modules`). Do not turn this into a BuildError.
- Enterprise addons live in `builds/branch-N/enterprise` (mounted at
  `/mnt/enterprise`). `docker_ctl.server_command` / `task_command` add it to
  `--addons-path` only when `enterprise=True`; pass the project flag through
  the builder and branch Start.
- Git credentials are stripped from URLs and passed through a temporary
  `GIT_ASKPASS` helper (`gitops._credentials` / `_git(credentials=...)`).
  Never put tokens back into clone/fetch arguments, logs or error messages;
  existing checkouts get their `origin` reset to the clean URL on sync.
- Custom domains are per branch (`Branch.domain`). A domain gets a
  loopback-only `Branch.gevent_port`; Docker publishes `8072/tcp` only on
  `127.0.0.1`. All nginx/certbot operations go through
  `app/services/nginx.py` (write config, `nginx -t`, rollback, reload).
  Domains require `proxy_mode` in the project Odoo configuration; use
  subdomains, never path prefixes.
- Development builds with tests enabled run **only the modules listed in
  branch Settings** (`--test-tags <modules>`); the base/web suite is
  deliberately skipped because its barcode/PDF/font tests need tooling the
  arm64 image may lack. Keep this scoping when touching `task_command` /
  builder test flags.
- Odoo >= 19 defaults `http_interface` to 127.0.0.1 - `server_command` must
  always pass `--http-interface 0.0.0.0` or the published branch port never
  answers.
- WebSocket shell needs the `docker` SDK exec socket
  (`terminal._socket._sock`); this is a private API but stable across the
  pinned `docker>=7.1,<8`.
- `install.sh` keeps an existing `.env` (secrets must not regenerate on
  update): its internal rsync excludes `.env .venv var .git __pycache__` —
  never remove `.env` from that exclude list.
- Firewall: branch ports and 8000 must be open; staging/prod safety relies
  on Mailpit capture only — no other outbound mail blocking exists.

## Definition of done for changes

1. `MOCK_DOCKER=1` boot, login, and exercise the touched flows.
2. `python -m compileall app` clean.
3. Docs updated when behavior/settings/routes change (README table,
   docs/API.md for endpoints, docs/RUNBOOK.md for ops, this file for
   invariants).
4. `install.sh` still idempotent if touched (`bash -n install.sh` minimum).
