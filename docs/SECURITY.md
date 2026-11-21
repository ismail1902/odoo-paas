# Security model

## Trust boundaries

| Zone | Reachable from | Notes |
|---|---|---|
| Control plane :8000 | the internet (firewall rule) | session-cookie login; keep it off the public internet if possible |
| Odoo branch ports 9000–9990 | the internet | these are the product; each runs the customer's Odoo |
| PostgreSQL | docker network only (not published) | branch containers connect by name; host access via `docker exec -it odoo-paas-pg psql -U odoo` |
| Mailpit 127.0.0.1:8025/1025 | host only (SSH tunnel) | captures all branch emails |
| Web shell (in-browser) | authenticated UI users | role- and stage-gated, never unauthenticated |

The control plane has root-equivalent power over the host (Docker socket).
Treat UI accounts as server administrators and prefer `ufw` to restrict
:8000 to your own IPs (`ufw allow from YOUR_IP to any port 8000`).

## Authentication & sessions

- Passwords: PBKDF2-SHA256, 260k iterations, per-user salt
  (`app/auth.py`). No external crypto dependency.
- Sessions: signed cookies (`itsdangerous`), `HttpOnly`, `SameSite=Lax`,
  7-day expiry, `SECRET_KEY` from `.env` (install.sh generates a 32-byte
  random value; rotation logs everyone out).
- First admin: seeded from `ADMIN_EMAIL`/`ADMIN_PASSWORD` only while the
  user table is empty; afterwards create users with `manage create-admin`.
- Optional TOTP 2FA (`app/twofa.py`): a correct password issues only a
  signed 5-minute pending cookie; the real session cookie is set after a
  valid 6-digit code or recovery code. Verification accepts a ±1 time-step
  window. Ten recovery codes are generated at enrolment, shown once, stored
  as SHA-256 hashes, and deleted when used. Disabling 2FA requires the
  account password. `manage reset-2fa --email` is the lockout path.

## HTTP security headers

Branch and control-plane nginx configs send:

- `Strict-Transport-Security: max-age=31536000`
  (HTTPS server blocks only)
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: camera=(self), geolocation=(self), microphone=(self),
  payment=(self), usb=()`

Odoo itself supplies `Content-Security-Policy: frame-ancestors 'self'`,
`X-Frame-Options: SAMEORIGIN` and `X-Content-Type-Options: nosniff`.
First-party camera/geolocation/microphone/payment access stays available for
Odoo features; third-party frames are blocked by the policy.

If **Cloudflare** proxies the domain, its edge HSTS setting can override the
origin header. A response of
`Strict-Transport-Security: max-age=0; includeSubDomains; preload` means HSTS
is disabled at Cloudflare. Set **SSL/TLS → Edge Certificates → HSTS** to a
positive max-age (6–12 months), or remove any Transform Rule that sets
`max-age=0`.

## Roles

`developer` < `tester` < `admin`.

| Capability | developer | tester | admin |
|---|---|---|---|
| View projects/branches/builds/logs/backups | yes | yes | yes |
| Deploy, start/stop/restart, create backups, settings | yes | yes | yes |
| Shell: development branch | yes | yes | yes |
| Shell: staging branch | no | yes | yes |
| Shell: production branch | no | no | yes |
| Restore backups | no | no | yes |
| Delete branches / projects | no | no | yes |

Known gap: any authenticated user can deploy to any stage, including
production (Odoo.sh-equivalent would restrict production deploys to
admins). If you need this stricter today, keep developer/tester accounts
out of the platform or deploy via admins only.

## Secrets inventory

| Secret | Where | Rotation |
|---|---|---|
| `SECRET_KEY` (session signing) | `.env` (mode 600) | change + `systemctl restart odoo-paas` (invalidates sessions) |
| `PG_PASSWORD` | `.env` + `odoo-paas-pg` | edit `.env`, re-run `install.sh` (it re-syncs the role password), then redeploy branches |
| Project webhook secrets | state DB, shown per project | re-create project or update row; update GitHub webhook |
| Repo token in project URL | state DB, shown per project | edit project settings |
| SMTP password (Odoo config) | state DB, `Project.odoo_config` (plaintext) | edit project settings |
| UI passwords | state DB (hashes only) | `manage set-password` |
| TOTP secrets | state DB (plaintext, needed to verify codes) | disable/re-enrol 2FA or `manage reset-2fa`; rotating `SECRET_KEY` does not change them |
| 2FA recovery codes | state DB (SHA-256 hashes only) | regenerate by disabling and re-enrolling 2FA |

Repository tokens are stored in project URLs, but git operations pass them
through a temporary `GIT_ASKPASS` helper instead of the command line.
Clone/fetch URLs are stripped of credentials before reaching git, and existing
checkouts use clean `origin` URLs. Tokens remain visible in the project
settings UI and the state database.

`.env` is excluded from rsync updates on the server (only created when
missing) and from git (`.gitignore`).

## Known properties (not vulnerabilities)

- Platform UI and branch URLs are plain HTTP. On shared networks, session
  cookies and Odoo logins travel unencrypted. Put a TLS reverse proxy (e.g.
  Caddy/nginx or an SSH tunnel) in front if that matters.
- Branch containers all share one PostgreSQL superuser (`odoo`): a
  malicious repo/`requirements.txt` is arbitrary code execution inside the
  branch container (expected — it is the customer's code) and can read other
  branches' databases. Run only repos you trust; use separate servers for
  mutually untrusted tenants.
- Branch containers have network access and no resource limits by default.
  Add `mem_limit`/`nano_cpus` in `docker_ctl.start_server_container` if
  needed.
- Project settings (including repo URL tokens and the SMTP password) are
  visible to every signed-in user; Odoo config is stored as plaintext JSON in
  the state database. Use dedicated least-privilege credentials.
- The web shell equals SSH access to the branch container (not the host).
- Mailpit stores copies of all outgoing mail unencrypted under the docker
  volume.
- TOTP secrets are stored unencrypted in the state database (they must be
  available to verify codes). Anyone who can read `state.db` can generate
  valid codes for enrolled accounts; protect the file like `.env`.

## Hardening checklist

- [ ] Put the control plane behind a domain/HTTPS from the topbar Settings
      page; after the automatic rebind it listens only on `127.0.0.1`. If you
      keep IP:8000, restrict it with `ufw` to your office/VPN IPs. Leave
      9000–9990 open only if branch Odoos are meant to be public.
- [ ] Strong admin password; no shared accounts; create separate users.
- [ ] Enable 2FA for admin accounts and store the recovery codes offline.
- [ ] `.env` mode 600, owned by root (install.sh does this).
- [ ] Keep the host patched; `sudo ./install.sh --rebuild-base` for Odoo
      security releases, then redeploy branches.
- [ ] Back up each PostgreSQL volume (`odoo-paas-pgdata`,
      `odoo-paas-pg15data`, `odoo-paas-pg17data`, `odoo-paas-pg18data`) +
      `/var/lib/odoo-paas/backups` off the server (the platform does not do
      off-site backups). Example cron:
      `0 3 * * * docker exec odoo-paas-pg pg_dumpall -U odoo | gzip > /root/pg16-$(date +\%F).sql.gz`
      and the same with `odoo-paas-pg15` / `pg15-...`, etc.
- [ ] Optional: fail2ban on sshd; disable root password SSH in favour of keys.
