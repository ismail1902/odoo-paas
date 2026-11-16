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
- After setting a domain, **Deploy** the branch so the loopback gevent port
  is published; websockets use `/websocket` on that port.
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
sudo ./install.sh --odoo-versions "17.0 18.0 19.0 20.0"   # build series
sudo ./install.sh --rebuild-base --odoo-versions "20.0"   # refresh one
```

Per-series requirements (install.sh picks the base automatically):

| Series | Python | PostgreSQL | Image base |
|---|---|---|---|
| 17.0, 18.0, 19.0 | 3.10–3.13 (Debian 3.11) | ≥ 13 | `debian:bookworm-slim` |
| 20.0 | ≥ 3.12 | ≥ 16 (`any_value()`) | `python:3.12-slim-bookworm` |

The shared `odoo-paas-pg` container runs PostgreSQL 16, which satisfies all
supported series.

If a project's base image was removed, deploys fail with "Base image for
Odoo X is not built on this host" — rebuild it or create the project on a
different version. Existing databases are **not** migrated between Odoo
series; export/import via standard Odoo upgrade tooling if ever needed, and
never point two Odoo series at the same database.

## Upgrading PostgreSQL

Odoo >= 20 requires PostgreSQL >= 16 (`any_value()`, and a hard check at
startup: "Postgres version … lower than minimum required"). The platform
container `odoo-paas-pg` is created from `postgres:16` by current installs;
older installs used `postgres:15` for Odoo <= 19. To upgrade with data:

```bash
# 1. dump everything (while containers may keep running)
docker exec odoo-paas-pg pg_dumpall -U odoo > /root/pg_dumpall.sql
# 2. swap the container (keeps odoo-paas-pgdata volume for rollback)
docker rm -f odoo-paas-pg
docker volume create odoo-paas-pgdata16
docker run -d --name odoo-paas-pg --restart unless-stopped \
  --network odoo-paas -e POSTGRES_USER=odoo -e POSTGRES_PASSWORD="$(grep -m1 '^PG_PASSWORD=' /opt/odoo-paas/.env | cut -d= -f2-)" \
  -v odoo-paas-pgdata16:/var/lib/postgresql/data postgres:16
# 3. wait, then restore
sleep 10
docker exec -i odoo-paas-pg psql -U odoo -d postgres < /root/pg_dumpall.sql
```

Then redeploy the branches. PG 15→16 is a plain dump/restore (no
`pg_upgrade` needed at this scale). If anything goes wrong, remove the new
container/volume and start `odoo-paas-pg` from the old `odoo-paas-pgdata`.

## Log locations

| What | Where |
|---|---|
| Build logs (host) | `/var/lib/odoo-paas/logs/build-<id>.log` |
| Odoo server logs | `docker logs odoo-paas-branch-<id>` |
| Platform service logs | `journalctl -u odoo-paas -f` |
| PostgreSQL | `docker logs odoo-paas-pg` |
| Mailpit UI | `http://127.0.0.1:8025` (tunnel) |
