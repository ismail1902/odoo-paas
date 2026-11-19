# Deployment

## Server requirements

| | Minimum | Recommended |
|---|---|---|
| OS | Ubuntu 22.04 or 24.04 (amd64/arm64) | Ubuntu 24.04 |
| RAM | 4 GB | 8 GB+ (each running Odoo branch ≈ 300–600 MB) |
| CPU | 2 vCPU | 4 vCPU |
| Disk | 40 GB SSD | 80 GB+ (image ≈ 3 GB, each branch repo+venv ≈ 300–600 MB) |
| Network | ports 22, 80, 443, 8000, 9000–9990 open | + ufw rules installed automatically |

Docker, Python 3, PostgreSQL and the Odoo base image are installed/created by
`install.sh`; nothing else is needed on a fresh server.

## Install

```bash
# from your machine
scp -r odoo-paas root@SERVER:/root/
ssh root@SERVER
cd /root/odoo-paas
sudo ./install.sh --public-host SERVER_IP \
     --admin-email you@example.com --admin-password 'a-strong-password'
```

`install.sh` (idempotent — re-run it to update):

1. apt prerequisites + Docker (get.docker.com when missing)
2. repo sync to `/opt/odoo-paas` (data lives in `/var/lib/odoo-paas`)
3. `.env` creation — random `SECRET_KEY` and `PG_PASSWORD` on first run only
4. Docker network `odoo-paas`, shared pgvector containers and
   `odoo-paas-mailpit` (127.0.0.1:8025/1025). `--postgres-versions "15 16 17 18"`
   (the default) provisions `odoo-paas-pg`, `odoo-paas-pg15`,
   `odoo-paas-pg17` and `odoo-paas-pg18`, each on its own data volume.
   PostgreSQL 18 mounts `/var/lib/postgresql` (its PGDATA is versioned);
   PostgreSQL 15–17 use `/var/lib/postgresql/data`. The installer enables the
   `vector` extension in `template1` and every branch database of each
   container. Re-running the installer on an older `postgres:16` deployment
   swaps the PG16 image in place (same major version, same volume); the
   `-trixie` tags match the current `postgres:16` image (glibc 2.41), while a
   volume created on bookworm can use `POSTGRES_IMAGE=pgvector/pgvector:pg16`.
5. base image(s) `odoo-paas/base:<version>` — one per Odoo version
   requested (`--odoo-versions "15.0 16.0 17.0 20.0"`; `--rebuild-base` to refresh).
   Series >= 20 are built on `python:3.12-slim-bookworm` because Odoo
   requires Python >= 3.12; older series use Debian's Python 3.11.
6. venv + `pip install -r requirements.txt`
7. systemd unit `odoo-paas` enabled and started; ufw rules for 8000 and
   9000–9990 when ufw is active
8. first admin user (only if none exists)

Useful flags: `--public-host`, `--admin-email`, `--admin-password`,
`--rebuild-base`, `--odoo-version 16.0` (single), `--odoo-versions "15.0 16.0 17.0 20.0"`
(several), `--postgres-versions "15 16 17 18"` (shared PG/pgvector containers).
Env equivalents: `PUBLIC_HOST`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`,
`ODOO_VERSION`, `ODOO_VERSIONS`, `POSTGRES_VERSIONS`, `PORT`,
`POSTGRES_IMAGE` (PG16 image; defaults to a pgvector tag matching the
existing volume's libc; changing it triggers a same-major container swap),
`PG_VOLUME` (PG16 data volume; detected from the running container when
omitted), `PG_CONTAINER`, `PG_CONTAINER_15`, `PG_CONTAINER_17`,
`PG_CONTAINER_18`.

If you did not pass an admin password, the installer generates one and prints
it once. Recover later with:

```bash
cd /opt/odoo-paas
.venv/bin/python -m app.manage create-admin --email you@x --password 'new'
# or reset:
.venv/bin/python -m app.manage set-password --email you@x --password 'new'
```

## First steps after install

1. Open `http://SERVER_IP:8000`, sign in.
2. Create a project: name + Odoo version + PostgreSQL version. Only Odoo
   versions with a built base image and PostgreSQL versions whose shared
   container is running are offered; the PostgreSQL dropdown is filtered to
   versions compatible with the selected Odoo series. The Git URL is optional
   — leave it empty to deploy base Odoo and add it later under **Project
   settings** (for example `https://github.com/acme/odoo-addons.git`).
3. In GitHub: Settings → Webhooks → Payload URL shown on the project page
   (`http://SERVER_IP:8000/webhooks/github/<project_id>` before a control-plane
   domain, `https://<domain>/webhooks/github/<project_id>` after), content type
   `application/json`, secret = the value shown on the project page.
4. Create a branch (git branch name + stage). The build starts automatically.
   Watch it under the branch's *History* tab / open the build for the live log.
5. The Odoo URL appears on the board: `http://SERVER_IP:<port>`.

### Control-plane domain and HTTPS

To stop using `http://SERVER_IP:8000`, sign in as an admin and open the topbar
**Settings** gear (`/settings`):

1. Enter the domain that should serve the platform UI, e.g.
   `paas.example.com`.
2. Point its DNS A/AAAA record at the server and make sure ports 80 and 443 are
   open on the host and in the cloud security list.
3. Save. nginx writes `/etc/nginx/conf.d/odoo-paas-control-plane.conf`,
   certbot obtains a Let's Encrypt certificate, and the platform schedules a
   short restart with `HOST=127.0.0.1` in `.env`.
4. After the restart, use `https://paas.example.com` and discard the IP:8000
   URL. The browser session is host-specific, so sign in again on the new
   domain.

If certbot cannot issue the certificate yet (DNS not propagated, port 80
blocked, rate limit), the domain remains HTTP-only and uvicorn stays on
`0.0.0.0` so IP:8000 access is not lost. Fix the DNS/firewall issue and save
the domain again to retry.

Use **Disable domain** to remove the nginx site and restore `HOST=0.0.0.0`.
The certificate stays on disk and can be reused. This setting is only for the
control plane; Odoo branch domains remain under each branch's Settings tab.
Update any GitHub webhook payload URLs to
`https://<domain>/webhooks/github/<project_id>` after switching, because the
old IP:8000 endpoint is no longer reachable.

### PostgreSQL compatibility

| Odoo series | PostgreSQL choices |
|---|---|
| 15.0–19.0 | 15, 16, 17, 18 |
| 20.0 | 16, 17, 18 |

The choice is made at project creation and copied to every branch. Branch
databases are never moved between PostgreSQL containers; create a new project
to use a different version. The dropdown hides versions whose container is
not provisioned. Every combination above was tested with base + mail, common
business modules, an update and a web login. Provision all with:

```bash
sudo ./install.sh --postgres-versions "15 16 17 18"
```

### Project settings

The gear button on a project page opens `/projects/<id>/settings` (a separate
page). It contains:

- display name, Git repository URL (optional) and Enterprise addons
  repository URL (optional), plus the private-repository credential guide;
  the Enterprise repository is cloned at the project's Odoo version branch
  (e.g. `17.0`) into each branch's `/mnt/enterprise` and added to the addons
  path when configured;
- the PostgreSQL version chosen at project creation (read-only; branch
  databases stay on that version's container);
- the Odoo server configuration: performance (workers, cron threads, request
  and memory limits, DB connections), HTTP/security (proxy mode, X-Sendfile,
  database-list visibility, unaccent), logging, SMTP, developer mode, and an
  **Advanced** field for any other server CLI option supported by the
  project's Odoo series.

Each option has an enable checkbox; disabled options use Odoo's default or the
platform default shown in the help text. Platform-managed flags (database,
addons path, data directory, HTTP port/interface, init/update/tests) are
rejected from the advanced field. Changes apply to all branches on their next
**Deploy** or **Start**; running containers keep their current command until
then.

### AI features (Odoo 19/20)

Odoo 19 and 20 AI features require the PostgreSQL
[pgvector](https://github.com/pgvector/pgvector) extension. All shared
containers (PostgreSQL 15, 16, 17 and 18) run pgvector images; the installer
enables `vector` in `template1` and every branch database of each container.
Verify with:

```bash
cd /opt/odoo-paas
.venv/bin/python -m app.manage doctor
# postgres 18:   ok, pgvector 0.x (odoo-paas-pg18)
# postgres 17:   ok, pgvector 0.x (odoo-paas-pg17)
# postgres 16:   ok, pgvector 0.x (odoo-paas-pg)
# postgres 15:   ok, pgvector 0.x (odoo-paas-pg15)
docker exec odoo-paas-pg18 psql -U odoo -d paas_<project>_<branch> \
  -tAc "select extversion from pg_extension where extname = 'vector'"
```

If a container still runs plain `postgres`, re-run `install.sh`; it swaps the
same-major image on the existing volume and enables the extension. New
installs provision PostgreSQL 15–18 by default.

### Custom domains and HTTPS

Use **subdomains per branch** rather than path prefixes:

```text
app.example.com       -> production
staging.example.com   -> staging
dev.example.com       -> development
```

Point the hostnames (or a wildcard `*.example.com`) at the server, open
**80/443** in the OCI security list and on the host firewall, and set
`ACME_EMAIL` in `/opt/odoo-paas/.env`. Then:

1. Enable **Proxy mode** in Project settings → Odoo configuration.
2. Open the branch → Settings → **Custom domain** and enter the hostname.
3. Deploy the branch so the loopback gevent port is published.

The platform writes `/etc/nginx/conf.d/odoo-paas-branch-<id>.conf`, tests it
with `nginx -t`, reloads nginx, and obtains a Let's Encrypt certificate with
certbot. The generated config follows Odoo's official
[Nginx HTTPS sample](https://www.odoo.com/documentation/20.0/administration/on_premise/deploy.html#https):
separate HTTP and websocket/gevent upstreams, `X-Forwarded-*` headers,
HTTPS with HSTS, and `/websocket` (plus legacy `/longpolling`) forwarding.

Odoo only starts the dedicated realtime listener when **HTTP workers > 0**
(multi-processing). With the default threaded server (`workers = 0`), Odoo
serves websockets (16+) or longpolling (15) on the main HTTP port, and the
generated nginx config routes the realtime paths there. The platform passes
`--longpolling-port 8072` to Odoo 15 and `--gevent-port 8072` to Odoo 16+.
If you change the project's HTTP workers, re-save each branch domain and
Deploy so nginx switches to the dedicated loopback port.
Certificates renew through certbot's systemd timer, with nginx reloaded by
the deploy hook.

`install.sh` installs nginx and certbot and opens 80/443 when ufw is active.
On an existing server, install them once with
`apt-get install -y nginx certbot` and open the ports in the OCI console.

Avoid `example.com/staging`-style prefixes: Odoo generates absolute URLs,
assets and cookies, so path-prefixed reverse proxying is fragile. If you only
have one hostname, use separate ports (the current `http://IP:9000` model)
or subdomains.

### Repository layout the platform expects

```
repo/
  <addon>/                 your Odoo modules (top level, as usual)
  requirements.txt         optional: extra pip packages
  <addon>/requirements.txt optional: per-module pip packages
```

Odoo core comes from the image; your checkout is added to `--addons-path`
after `/opt/odoo/addons`. Databases are named `paas_<project>_<branch>`.
Enterprise addons are cloned separately into `builds/branch-N/enterprise`
(mounted at `/mnt/enterprise`) and are not mixed into your repository.

### Private repositories

The platform clones over plain HTTPS, so a private repo URL must embed
credentials. Use a **read-only** token scoped to that repository only:

```text
# GitHub - fine-grained PAT (Contents: Read-only), recommended
https://<github-username>:<TOKEN>@github.com/<owner>/<repo>.git
# GitHub - token as username also works (classic or fine-grained PAT)
https://<TOKEN>@github.com/<owner>/<repo>.git

# GitLab - personal access token (read_repository) or deploy token
https://oauth2:<TOKEN>@gitlab.com/<group>/<repo>.git
https://<deploy-user>:<deploy-token>@gitlab.com/<group>/<repo>.git

# Bitbucket - app password (or access token)
https://<username>:<app-password>@bitbucket.org/<workspace>/<repo>.git
https://x-token-auth:<access-token>@bitbucket.org/<workspace>/<repo>.git
```

Paste the whole URL into the repository field in **Project settings** (or the
**New project** form). The URL (token included) is stored in the platform
database and shown on the project page, so prefer a dedicated least-privilege
token. If a token expires, builds fail at `cloning repository` with
`git ls-remote failed: Authentication failed`. The same guidance is shown
inline in the **New project** form and the project settings page.

### Behind NAT (no public IP on the interface)

If `ip addr` shows only a private address, forward ports on the router:
`8000` (control plane) and the branch range `9000–9990` (or just the ports
in use) to the server's private IP. Without forwarding, GitHub webhooks
cannot reach the platform and branch Odoos are not publicly reachable;
everything still works locally and through an SSH tunnel:

```bash
ssh -N -L 8000:127.0.0.1:8000 -L 9000:127.0.0.1:9000 you@server
# then browse http://127.0.0.1:8000
```

### Staging with production data

Staging branches start empty. To seed one: open the production branch →
Backups → *Create backup*, then the staging branch → Backups → *Restore*
(admin only). The container stays stopped after restore; press Start/Deploy.

## Updating the platform

```bash
cd /root/odoo-paas && git pull        # or re-scp the directory
sudo ./install.sh                      # rsyncs to /opt/odoo-paas, restarts service
```

Running branch containers are not touched. New base-image content (Odoo
security updates) needs a rebuild of the wanted versions:

```bash
sudo ./install.sh --rebuild-base --odoo-versions "15.0 16.0 17.0 20.0"
# then redeploy branches (Deploy button) to pick up the new image
```

Or from the UI: open a branch → **Update Odoo + Deploy**. This rebuilds the
base image for that branch's Odoo version from the latest source, runs
`-u all` when the database already exists, and deploys the branch in one
build (the single worker serializes it with other builds).

Base images are per Odoo version (`odoo-paas/base:17.0`, `…:20.0`, …);
projects choose one at creation. Existing projects keep their version —
create a new project (or branch roster) to move to another series.

## Manual (non-installer) operation

Everything is plain commands if you prefer:

```bash
cd /opt/odoo-paas
sudo -u root .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The control plane must run as root (Docker socket + host dirs). `MOCK_DOCKER=true`
runs it without Docker for UI work (macOS default).

## Uninstall

```bash
systemctl disable --now odoo-paas
rm /etc/systemd/system/odoo-paas.service
docker rm -f odoo-paas-pg odoo-paas-pg15 odoo-paas-pg17 odoo-paas-pg18 \
  odoo-paas-mailpit $(docker ps -aq -f name=odoo-paas-branch-)
docker volume rm odoo-paas-pgdata odoo-paas-pg15data odoo-paas-pg17data \
  odoo-paas-pg18data   # destroys ALL branch databases
rm -rf /opt/odoo-paas /var/lib/odoo-paas
```
