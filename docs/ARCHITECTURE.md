# Architecture

## Topology

```
                      ┌──────────────────────────── control plane (systemd: odoo-paas) ─┐
                      │  uvicorn app.main:app                ┌─ SQLite state.db        │
 Browser ─── HTTP ───►│  web.py (pages)  api.py (hooks/ws)   │─ builds/branch-N/{repo,│
                      │  worker.py (1 build at a time) ──────┤    venv, filestore}    │
                      └───────────────┬──────────────────────┴─ backups/ logs/         │
                                      │ docker SDK / docker exec                          │
              ┌───────────────────────┼──────────────────────────────────────┐
              ▼                       ▼                                      ▼
   odoo-paas-pg (PostgreSQL 15)  odoo-paas-mailpit              odoo-paas-branch-N ×N
   one DB per branch              SMTP :1025, UI :8025           base image + repo/filestore/venv
   docker-network only            network-reachable only         published 0.0.0.0:<branch port>
```

- Docker network `odoo-paas` (bridge) connects Postgres, Mailpit and all
  branch containers. Branch containers reach Postgres as `odoo-paas-pg:5432`
  and SMTP as `odoo-paas-mailpit:1025`.
- PostgreSQL is not published on the host at all (avoids clashes with a
  distro PostgreSQL): branch containers reach it through the Docker network,
  the platform host-side code uses `docker exec`.
- Branch ports come from the pool `PORT_RANGE_START..PORT_RANGE_END`
  (default 9000–9990), one per branch, published on `0.0.0.0`.

## State (SQLite via SQLModel)

```
User      (email, role: admin|developer|tester)
Project   (slug, repo_url, webhook_secret)
Branch    (project_id, name, stage: production|staging|development,
           port, db_name, status, install_modules, tests_enabled, update_on_deploy)
Build     (branch_id, status: queued|running|succeeded|failed, step, commit_*,
           log_path, started_at, finished_at)
Backup    (branch_id, kind: manual|auto|update, db_path, filestore_path,
           size_bytes, created_at)
Event     (project_id, branch_id?, build_id?, kind, message, created_at)
Setting   (key, value)  - reserved
```

The state DB is small and derivable (except Event history): on a lost
server, recreate projects/branches and rebuild — databases live in the
PostgreSQL volume, files under `DATA_DIR`.

## Build pipeline (`app/services/builder.py`)

1. **queued → claimed** by the single worker thread (`worker.py`), 2 s poll.
2. **cloning** — `git clone --branch <branch> --single-branch` or
   `git fetch origin <sha> && checkout --force <sha>` into
   `builds/branch-N/repo` (`gitops.sync`). Commit metadata is stored on the
   Build row. A project with no repo URL clears any previous checkout and
   continues with base Odoo; the URL can be added later.
2b. **enterprise addons** — when `Project.enterprise_repo_url` is set, the
   repository is cloned at the branch matching the project's Odoo version
   into `builds/branch-N/enterprise` (mounted at `/mnt/enterprise`) and
   added to `--addons-path`. A missing version branch fails the build.
3. **installing requirements** — one-off container runs
   `python3 -m venv --system-site-packages /mnt/venv` (first time only) then
   `pip install -r requirements.txt` plus, if present, every
   `<module>/requirements.txt` two levels deep (`docker_ctl.ensure_venv`).
   The base image already has Odoo core dependencies system-wide.
4. **initializing database** — first build: `odoo-bin --no-http
   --stop-after-init -i <install_modules|base>` (demo data on development
   branches only; `--test-enable` when the branch has tests enabled). Later
   builds: `-u all` only when *Update modules on deploy* is on, else skip.
5. **starting** — (re)create `odoo-paas-branch-N` from the shared volumes,
   `restart=unless-stopped`, then HTTP-poll `http://127.0.0.1:<port>/web/login`
   for up to 90 s.
6. Success/failure updates the Build + Branch rows and writes an Event.

Module **version bumps** in the user repository behave like Odoo.sh: the
deploy itself does not auto-update modules unless the branch setting
`update_on_deploy` is on (`-u all`). (True per-module version-diff detection
is a future improvement; see RUNBOOK for the manual workflow.)

Failure handling: the branch container is stopped, Build → failed, Branch →
failed, Event written. A platform restart marks stale `running` builds failed
(`worker._recover_stale_builds`).

## Odoo server configuration

`Project.odoo_config` stores a JSON object with the Odoo server options the
user enabled on the project settings page (`app/odoo_config.py`). The page
exposes curated groups — performance, HTTP/security, logging, SMTP and
developer options — plus an advanced field for any other server CLI option.
Platform-managed flags (`-d`, `--addons-path`, `--data-dir`, `--db-filter`,
`--http-interface`, `--http-port`, init/update/test flags) cannot be
overridden.

`docker_ctl.server_command(branch, project.odoo_config)` merges the enabled
options with those fixed flags when a branch container is created. Because the
configuration is project-wide, a change takes effect for each branch the next
time it is Deployed or Started; running containers keep their current command
until then.

## WebSockets (`app/api.py`)

- `GET /ws/logs/{branch_id}` — text stream. If the branch container is
  running it streams `docker logs -f`; otherwise it tails the latest build
  log file. Read-only.
- `GET /ws/shell/{branch_id}` — binary stream bridged to `docker exec`
  (TTY) using the `docker` SDK's exec socket. Client sends raw keystrokes as
  binary frames and JSON text frames `{"type":"resize","rows":..,"cols":..}`.
  Role-gated (`can_open_shell`) and refused unless the container runs.

Both endpoints resolve the session cookie with `current_user_ws`.

## Monitoring (`app/services/monitor.py`, `app/static/js/monitor.js`)

The Monitor tab polls `GET /api/monitor/{branch_id}` every 2 s. The endpoint
returns host metrics (psutil: CPU, load average, memory/swap, data-dir disk,
uptime) and container metrics (docker stats: CPU, memory, PIDs, cumulative
network and block I/O). The browser keeps a rolling 90-sample window and
derives I/O rates by differencing the cumulative counters. Chart.js renders
CPU, memory, network and disk I/O charts plus a disk gauge; the insights
panel summarizes container CPU/RAM, host load and disk headroom. Chart.js is
loaded from the CDN only on the Monitor tab.

## Backups & retention (`app/services/backups.py`)

`pg_dump -Fc` inside the Postgres container (streamed to
`backups/project-P/branch-B/db-<ts>.dump`) + tar.gz of the branch filestore.
Retention (hourly from the worker + `python -m app.manage retention`):

- nothing younger than 3 days is deleted,
- keep first backup per day for 7 days, per ISO week for 4 weeks, per month
  for 3 months.

Restore: stop container → kill DB connections → drop + recreate → `pg_restore`
→ extract filestore → branch stays stopped until Start/Deploy (identical to
Odoo.sh, where restores are followed by a restart).

## Docker user model

Containers run as `odoo` (uid/gid 1000, see Dockerfile). The control plane
chowns each `builds/branch-N` directory to 1000:1000 (`docker_ctl._make_container_writable`)
so bind mounts are writable, while the host-side worker (root) reads Git
checkouts. `git config --global --add safe.directory '*'` is set by
install.sh for the root worker.

## Key design decisions

| Decision | Why |
|---|---|
| One shared Postgres container | Cheapest on a single small server; one DB per branch gives the same isolation semantics as Odoo.sh. |
| Per-branch venv (not per-branch image) | Repos installing extra pip deps keep working; base image stays shared and cached. |
| `--system-site-packages` venv | Odoo core deps live in the image; per-branch pip only adds repo-specific packages. |
| SQLite for platform state | Single writer (worker + uvicorn threadpool), zero ops. |
| MOCK_DOCKER flag | The pipeline is Linux-only; the UI is developable on macOS. |
| IP:port URLs, no TLS | Deliberate simplification — see README non-goals. |
| Base image pinned to one Odoo version | Matches the scope: one Odoo series per server; `--odoo-version` + `BASE_IMAGE` allow others. |
