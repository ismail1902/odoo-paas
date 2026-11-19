# Runbook

Day-2 operations. All commands assume `cd /opt/odoo-paas` on the server.
Prefix Python commands with `.venv/bin/python -m app.manage` (aliased below
as `manage`).

## Health & diagnostics

```bash
systemctl status odoo-paas            # control plane
manage doctor                         # paths, docker, postgres, worker
curl -s localhost:8000/healthz        # {"ok":true,"docker":true,...}
docker ps                             # branch containers + shared containers
docker logs -f odoo-paas-branch-<id>  # raw container output
tail -f /var/lib/odoo-paas/logs/build-<id>.log
```

## Common tasks

| Task | How |
|---|---|
| Deploy / redeploy a branch | UI: branch → Deploy. Or push to the git branch (webhook). |
| Update Odoo core to latest | branch → **Update Odoo + Deploy** (rebuilds `odoo-paas/base:<version>` from the latest source, runs `-u all` when the DB exists, then deploys). Take a production backup first. |
| Add a domain / HTTPS | Enable Proxy mode in Project settings, set the branch's **Custom domain**, ensure DNS and 80/443 are open, then Deploy. Config: `/etc/nginx/conf.d/odoo-paas-branch-<id>.conf`. |
| Inspect live resource usage | branch → **Monitor**: CPU/RAM/network/disk charts poll `/api/monitor` every 2 s; use **Pause** to freeze the view. |
| Restart a stuck Odoo | UI: branch → Restart. (Start re-creates the container.) |
| Stop everything for a branch | UI: branch → Stop. `docker stop odoo-paas-branch-<id>` equivalent. |
| Move branch to another stage | branch → Settings → *Stage* → Move. The DB name follows the project+branch name, so data is kept. |
| Change modules installed on first init | branch → Settings → *Modules to install* **before the first build**; afterwards use `-u all` (toggle update_on_deploy) or the shell. |
| Install a module later | shell → `odoo -d $PGDATABASE --no-http --stop-after-init -i <module>` (from the branch *Shell* tab). |
| Update all modules after pulling code | branch → Settings → enable *Update modules on deploy* → Deploy. |
| Change control-plane domain | topbar **Settings** gear → enter domain → save (nginx + certbot + loopback rebind). |
| Take a backup now | branch → Backups → *Create backup* (or `manage backup-all`). |
| Download a backup | branch → Backups → Download (db dump only; the filestore tarball sits next to it under `/var/lib/odoo-paas/backups/`). |
| Restore a backup | branch → Backups → Restore (admin). Container stops; press Start when ready. |
| Read captured emails | SSH tunnel `ssh -L 8025:127.0.0.1:8025 root@SERVER` then http://127.0.0.1:8025 |
| psql into a branch DB | branch → Shell → `psql` (PG* env vars are set). |
| Change Odoo server configuration | Project page → ⚙ **Settings** → *Odoo configuration* → enable/set options → Save → Deploy or Start the branch. |
| Create/inspect platform users | `manage create-admin/set-password`; roles live in the `user` table (editable via SQLite if needed). |

## Troubleshooting

### Build failed — where do I look?
Branch → History → click the build. The log shows the failing step
(`cloning`, `installing requirements`, `initializing database`, `starting
server`). Common causes:

- **cloning**: wrong repo URL, private repo (use the tokenized URL form in
  docs/DEPLOYMENT.md → Private repositories), no repository configured (add
  one in Project settings and Deploy), repository/branch not pushed yet
  (deploys base Odoo instead), force-push rewrote history.
- **enterprise addons**: `syncing enterprise addons` failed. The Enterprise
  repository must have a branch named exactly like the project's Odoo version
  (`17.0`, `19.0`, …); private repositories need a read-only token in the URL.
- **installing requirements**: a repo `requirements.txt` needs a system
  library absent from the base image → add it in
  `docker/odoo-base/Dockerfile` and `sudo ./install.sh --rebuild-base`.
- **initializing database**: Python syntax errors / bad manifest in an
  addon, missing `depends`, or the module cannot be installed without demo
  data. Re-run by fixing the code and pressing Deploy. A database left over
  from a failed init (empty, no Odoo tables) is detected and initialized on
  the next deploy automatically. Tests (development + enabled) run only for
  the modules listed in Settings (`--test-tags`), never for the base/web
  suites.
- **starting server**: port already bound (rare — allocation is persisted),
  Odoo crash at boot (see container log), or the addon registry is broken
  (roll back the last commit).

### "I pushed but my module is not there"

Modules listed in branch → Settings are installed automatically: the next
deploy installs any configured module that is not installed yet (including
the first deploy after pushing to a previously empty repository, which had
started with base modules only). If the list itself changed, redeploy; this
does not require "Update modules on deploy".

### "Docker is not available"
Control plane cannot reach `/var/run/docker.sock`: `systemctl status docker`,
`docker info`. The service must run as root (install.sh does this).

### Domain / HTTPS not working

- `Custom domain` requires **Proxy mode** in Project settings.
- DNS must resolve the hostname to this server and ports **80/443** must be
  open in the OCI security list and host firewall.
- `nginx -t` must pass; the platform rolls back invalid configs.
- certbot failures usually mean DNS is not pointing at the server yet or
  port 80 is blocked. Fix DNS/firewall, then save the domain again to retry.
- After setting a domain, **Deploy** the branch so the loopback realtime port
  is published. The realtime port is used only when the project's Odoo config
  has **HTTP workers > 0**; with the default threaded server (`workers = 0`)
  realtime uses the main HTTP port. Odoo 15 uses longpolling
  (`--longpolling-port`); Odoo 16+ uses websockets (`--gevent-port`).
  If you change workers, re-save the domain and Deploy.
- Logs: `/var/log/nginx/odoo-paas-<branch-id>.{access,error}.log`.

### Odoo container exits after a project-settings change

Check `docker logs odoo-paas-branch-<id>`. An unknown option or an invalid
value in **Advanced** is passed straight to Odoo and can prevent startup; fix
or disable it in project settings and Deploy again. Platform-managed options
(HTTP port/interface, database, addons, data dir, init/update/tests) are
rejected by the form.

### Container starts but URL 502s / times out
1. `docker logs --tail 100 odoo-paas-branch-<id>`.
2. Port reachable? `curl -sI http://127.0.0.1:<port>/web/login`.
3. Firewall: `ufw status` (8000 and 9000–9990 must be allowed).
4. DB filter: the server runs with `--db-filter '^<db>$'`; hitting the
   container with another Host header shows the database manager instead of
   the DB — that is expected.

### Shell tab greyed out
Stage/role gate: development = any role, staging = tester+, production =
admin. Also: the container must be running.

### PDF report printing fails
Odoo needs `wkhtmltopdf`. On amd64 servers the base image installs the
distro package automatically. On arm64 there is no Debian package; pass a
community arm64 `.deb` URL at build time:

```bash
sudo ./install.sh --rebuild-base --wkhtmltopdf-deb https://…/wkhtmltox_arm64.deb
# then redeploy branches
```

(The well-known `wkhtmltox_0.12.6.1-2.jammy_amd64.deb` from the wkhtmltopdf
GitHub releases only works on amd64.) Without it, HTML reports still work,
only PDF printing is unavailable.

### Disk filling up
- Backups: retention runs hourly; force with `manage retention`. Old
  branches' repos/venvs are removed on branch deletion.
- Docker: `docker system df`; dangling images from base rebuilds —
  `docker image prune`.
- Build logs: `/var/lib/odoo-paas/logs/` — safe to delete old files.

### Platform DB corrupted / accidental state loss
Branch databases are untouched in the Postgres volume. Recreate projects and
branches with the **same slug/branch name** → the same DB name is computed →
the data is reused on next build (`database exists` path).

## Production rollback semantics (important)

Odoo.sh's automatic rollback-on-failed-update is **not implemented**. The
safe workflow here is:

1. Before a risky deploy on production: branch → Backups → Create backup.
2. If the deploy fails: fix forward, or Restore that backup and redeploy the
   previous commit.

Backups are cheap; take one before every production `-u all` deploy.

## Odoo versions

The Odoo version is chosen **per project** when it is created; branches copy
it at creation. The dropdown lists every base image built on the host:

```bash
sudo ./install.sh                                           # all 15.0–20.0
sudo ./install.sh --odoo-versions "17.0 20.0"               # build a subset
sudo ./install.sh --rebuild-base --odoo-versions "20.0"     # refresh one
```

A default install builds all six images (roughly 20 GB). Set
`ODOO_VERSIONS` or pass `--odoo-versions` to limit the set.

Per-series requirements (install.sh picks the base automatically):

| Series | Python | PostgreSQL | Image base |
|---|---|---|---|
| 15.0 | 3.8–3.10 (Python 3.9) | ≥ 10 | `python:3.9-slim-bookworm` |
| 16.0 | 3.8–3.10 (Python 3.9) | ≥ 12 | `python:3.9-slim-bookworm` |
| 17.0, 18.0, 19.0 | 3.10–3.13 (Debian 3.11) | ≥ 13 | `debian:bookworm-slim` |
| 20.0 | ≥ 3.12 | ≥ 16 (`any_value()`) | `python:3.12-slim-bookworm` |

The shared containers run PostgreSQL 15, 16, 17 and 18. PostgreSQL 16 is the
default for new projects; the project form offers PG15–18 for Odoo 15–19 and
PG16–18 for Odoo 20. All combinations are tested with base + mail, common
business modules, an Odoo update and a web login. A PG17→PG18 dump/restore
of Odoo 18 (current 18.0 image) also passed; very old pre-fix Odoo 18 dumps
may still contain the redundant `channel_type` constraint that PG18 rejects.

If a project's base image was removed, deploys fail with "Base image for
Odoo X is not built on this host" — rebuild it or create the project on a
different version. Existing databases are **not** migrated between Odoo
series; export/import via standard Odoo upgrade tooling if ever needed, and
never point two Odoo series at the same database.

## Control-plane domain and bind address

Admins use the topbar **Settings** gear (`/settings`). Saving a domain writes
`/etc/nginx/conf.d/odoo-paas-control-plane.conf`, obtains a certificate with
certbot, and changes `/opt/odoo-paas/.env`:

```text
HOST=127.0.0.1
```

The systemd unit starts uvicorn with `${HOST}`. On the next restart uvicorn
only accepts connections from nginx on loopback; the public IP:8000 endpoint
is closed. Sessions are host-specific, so users sign in again on the new
domain. Removing the domain restores `HOST=0.0.0.0`, removes the nginx site,
and makes IP:8000 available again after the restart.

Check the current state:

```bash
grep '^HOST=' /opt/odoo-paas/.env
ss -ltnp | grep ':8000'
sudo nginx -t
sudo systemctl status odoo-paas
ls -l /etc/nginx/conf.d/odoo-paas-control-plane.conf
```

If HTTPS is still pending, the platform deliberately leaves `HOST=0.0.0.0`
so IP access remains available. Fix DNS/ports and save the domain again.
Do **not** manually set `HOST=127.0.0.1` without a working nginx control-plane
site, or the UI will be unreachable from the network.

The control-plane certificate is separate from branch certificates. Branch
domains remain under each branch's **Settings** tab. After switching, update
GitHub webhook payload URLs to the control-plane domain; the old IP:8000
endpoint is closed.

## PostgreSQL containers and project choices

Each project chooses a PostgreSQL/pgvector version at creation; every branch
copies it. The platform provisions one shared container per version:

| PG | Container | Volume | Image | Volume mount |
|---|---|---|---|---|
| 18 | `odoo-paas-pg18` | `odoo-paas-pg18data` | `pgvector/pgvector:pg18-trixie` | `/var/lib/postgresql` |
| 17 | `odoo-paas-pg17` | `odoo-paas-pg17data` | `pgvector/pgvector:pg17-trixie` | `/var/lib/postgresql/data` |
| 16 | `odoo-paas-pg` | `odoo-paas-pgdata` | `pgvector/pgvector:pg16-trixie` | `/var/lib/postgresql/data` |
| 15 | `odoo-paas-pg15` | `odoo-paas-pg15data` | `pgvector/pgvector:pg15-trixie` | `/var/lib/postgresql/data` |

PostgreSQL 18 uses the upstream image's versioned PGDATA
(`/var/lib/postgresql/18/docker`), so its volume mounts the parent directory;
15–17 mount `/var/lib/postgresql/data`.

Compatible Odoo versions: PostgreSQL 15–18 for Odoo 15–19, PostgreSQL 16–18
for Odoo 20. The New project dropdown hides PG versions whose container is
not running. Provision all four with:

```bash
sudo ./install.sh --postgres-versions "15 16 17 18"
```

A branch database lives in exactly one container. It is never moved between
PostgreSQL versions; create a new project to use a different version. Dumps,
restores and deletions use `docker_ctl.postgres_container(branch)`, so keep
the `Project.postgres_version` / `Branch.postgres_version` fields intact.

## pgvector (Odoo 19/20 AI)

Odoo 19/20 AI features require PostgreSQL's `vector` extension (pgvector).
Both shared containers run pgvector images; `install.sh` enables `vector` in
`template1` (so new databases inherit it) and in every existing database of
each container. `docker_ctl.ensure_database` also re-ensures it on every
deploy. The installer picks the `pg16`/`pg15` tag for glibc 2.36 (bookworm)
containers and `-trixie` otherwise; set `POSTGRES_IMAGE` to override PG16.

```bash
cd /opt/odoo-paas
.venv/bin/python -m app.manage doctor
# postgres 18:   ok, pgvector 0.x (odoo-paas-pg18)
# postgres 17:   ok, pgvector 0.x (odoo-paas-pg17)
# postgres 16:   ok, pgvector 0.x (odoo-paas-pg)
# postgres 15:   ok, pgvector 0.x (odoo-paas-pg15)

docker exec odoo-paas-pg psql -U odoo -d postgres -tAc \
  "select datname from pg_database where datistemplate = false" \
  | while read -r db; do
      printf '%s: ' "$db"
      docker exec odoo-paas-pg psql -U odoo -d "$db" -tAc \
        "select coalesce((select extversion from pg_extension where extname = 'vector'), 'missing')"
    done
```

Use `odoo-paas-pg15`, `odoo-paas-pg17` or `odoo-paas-pg18` instead of
`odoo-paas-pg` to inspect another container. If `pg_available_extensions` has
no `vector`, the container still runs plain `postgres`. Re-run `install.sh`
(it safely swaps same-major images on the existing volume and enables the
extension), or recreate it manually:

```bash
docker rm -f odoo-paas-pg
docker run -d --name odoo-paas-pg --restart unless-stopped \
  --network odoo-paas -e POSTGRES_USER=odoo -e POSTGRES_PASSWORD="$(grep -m1 '^PG_PASSWORD=' /opt/odoo-paas/.env | cut -d= -f2-)" \
  -v odoo-paas-pgdata:/var/lib/postgresql/data pgvector/pgvector:pg16-trixie
```

### Collation version mismatch

PostgreSQL records the libc collation version when a database is created. If
the container image changes OS (bookworm/glibc 2.36 ↔ trixie/glibc 2.41),
every connection logs:

```text
database "..." has a collation version mismatch
```

Pick the matching image tag: `pgvector/pgvector:pg16` for bookworm volumes,
`pgvector/pgvector:pg16-trixie` for trixie volumes (use the equivalent
`pg15`/`pg17`/`pg18` tags for the other containers; PG18 still mounts the
volume at `/var/lib/postgresql`). Do **not** just run
`ALTER DATABASE ... REFRESH COLLATION VERSION` on a volume that was used by
the other libc: text indexes may have been written with a different sort
order. The safe fix is a dump/restore into a fresh volume. Keep the same
volume name (or set `PG_VOLUME`); `install.sh` reuses the running container's
volume automatically:

```bash
# stop the platform and branch containers first
docker exec odoo-paas-pg pg_dumpall -U odoo | gzip > /root/pg_dumpall.sql.gz
# optional rollback copy of the raw volume
docker run --rm -v odoo-paas-pgdata:/from -v odoo-paas-pgdata-old:/to \
  alpine sh -c 'cp -a /from/. /to/'
docker rm -f odoo-paas-pg
docker volume rm odoo-paas-pgdata
docker run -d --name odoo-paas-pg --restart unless-stopped \
  --network odoo-paas -e POSTGRES_USER=odoo -e POSTGRES_PASSWORD="$(grep -m1 '^PG_PASSWORD=' /opt/odoo-paas/.env | cut -d= -f2-)" \
  -v odoo-paas-pgdata:/var/lib/postgresql/data pgvector/pgvector:pg16-trixie
sleep 10
zcat /root/pg_dumpall.sql.gz | docker exec -i odoo-paas-pg psql -U odoo -d postgres
```

`CREATE ROLE odoo ... already exists` and `database "odoo" already exists`
during the restore are expected; the data still restores. Re-enable pgvector
in `template1` and every database afterwards (see above).

## Upgrading the PostgreSQL 16 container

Odoo >= 20 requires PostgreSQL >= 16 (`any_value()`, and a hard check at
startup: "Postgres version … lower than minimum required"). The platform
container `odoo-paas-pg` is created from `pgvector/pgvector:pg16-trixie` by
current installs; older installs used `postgres:15/16`. The separate
`odoo-paas-pg15`, `odoo-paas-pg17` and `odoo-paas-pg18` containers keep those
versions and are not upgraded by this procedure. To upgrade the PG16
container with data:

```bash
# 1. dump everything (while containers may keep running)
docker exec odoo-paas-pg pg_dumpall -U odoo > /root/pg_dumpall.sql
# 2. swap the container (keeps odoo-paas-pgdata volume for rollback)
docker rm -f odoo-paas-pg
docker volume create odoo-paas-pgdata16
docker run -d --name odoo-paas-pg --restart unless-stopped \
  --network odoo-paas -e POSTGRES_USER=odoo -e POSTGRES_PASSWORD="$(grep -m1 '^PG_PASSWORD=' /opt/odoo-paas/.env | cut -d= -f2-)" \
  -v odoo-paas-pgdata16:/var/lib/postgresql/data pgvector/pgvector:pg16-trixie
# 3. wait, then restore
sleep 10
docker exec -i odoo-paas-pg psql -U odoo -d postgres < /root/pg_dumpall.sql

# 4. ensure the extension in restored databases
docker exec odoo-paas-pg psql -U odoo -d template1 -c "CREATE EXTENSION IF NOT EXISTS vector"
docker exec odoo-paas-pg psql -U odoo -d postgres -tAc \
  "select datname from pg_database where datistemplate = false" \
  | while read -r db; do
      docker exec odoo-paas-pg psql -U odoo -d "$db" -c "CREATE EXTENSION IF NOT EXISTS vector"
    done
```

Then redeploy the PG16 branches. PG 15→16 is a plain dump/restore (no
`pg_upgrade` needed at this scale). If anything goes wrong, remove the new
container/volume and start `odoo-paas-pg` from the old `odoo-paas-pgdata`.

To move a project between PostgreSQL versions, create a new project on the
target version and restore its branch dumps into the new project's databases.
Do not edit `Branch.postgres_version` in place: the branch metadata and its
database must stay on the same container.

## Log locations

| What | Where |
|---|---|
| Build logs (host) | `/var/lib/odoo-paas/logs/build-<id>.log` |
| Odoo server logs | `docker logs odoo-paas-branch-<id>` |
| Platform service logs | `journalctl -u odoo-paas -f` |
| PostgreSQL 18 | `docker logs odoo-paas-pg18` |
| PostgreSQL 17 | `docker logs odoo-paas-pg17` |
| PostgreSQL 16 | `docker logs odoo-paas-pg` |
| PostgreSQL 15 | `docker logs odoo-paas-pg15` |
| Mailpit UI | `http://127.0.0.1:8025` (tunnel) |
