# API reference

Base URL: `http://SERVER_IP:8000`. Browser routes are session-cookie
authenticated; `/webhooks/…` uses HMAC; `/healthz` is open. Accounts with
TOTP 2FA enabled complete a second step at `/login/2fa` before the session
cookie is issued.

## GitHub webhook

```
POST /webhooks/github/{project_id}
```

Headers: `X-Hub-Signature-256: sha256=<hex>` (HMAC-SHA256 over the raw body
with the project's webhook secret — shown on the project page),
`X-GitHub-Event: push|ping`.

Behavior:

- `ping` → `200 {"ok": true}`.
- `push` to a branch that is configured for this project → a Build is
  queued, branch status becomes `building` → `202 {"queued": <build_id>}`.
  The commit sha from the payload (`after`) is checked out exactly.
- Unknown project → `404 {"error": "unknown project"}`; bad signature →
  `401 {"error": "invalid signature"}`; invalid JSON → `400`; non-branch
  ref / unconfigured branch → `200 {"skipped": "..."}`.

## Health

```
GET /healthz → {"ok": true, "app": "Odoo PaaS", "mock_docker": false, "docker": true}
```

## Metrics (JSON)

```
GET /api/monitor/{branch_id}          (session cookie required)
```

```json
{
  "timestamp": 1763130000.0,
  "host":      {"cpu_percent": 4.2, "cpu_count": 4,
                "load_1": 0.4, "load_5": 0.3, "load_15": 0.2,
                "uptime": 864000,
                "mem_used": 812345678, "mem_total": 8589934592, "mem_percent": 9.5,
                "swap_used": 0, "swap_total": 0, "swap_percent": 0.0,
                "disk_used": 17345678901, "disk_total": 53687091200,
                "disk_percent": 32.3},
  "state":     "running",
  "container": {"cpu_percent": 1.1, "mem_used": 402653184, "mem_limit": 8589934592,
                "mem_percent": 4.7, "pids": 24,
                "net_rx": 1234567, "net_tx": 7654321,
                "blk_read": 12345678, "blk_write": 23456789}
}
```

`container` is `null` when the container is missing/stopped. `net_rx`,
`net_tx`, `blk_read` and `blk_write` are cumulative counters since container
start; derive rates by differencing them. The Monitor tab polls this endpoint
every 2 s and keeps a 90-sample rolling window in the browser.

## WebSockets

### `WS /ws/logs/{branch_id}` — live logs (read-only)

Text frames. While the branch container runs: `docker logs -f` stream.
Otherwise: live tail of the latest build log file. Close codes: 4401 (login
required), 4404 (branch not found).

### `WS /ws/shell/{branch_id}` — interactive shell

Binary frames = raw PTY bytes (both directions). The client may send JSON
text frames to control the TTY:

```json
{"type": "resize", "rows": 40, "cols": 120}
```

Close codes: 4401 login required · 4403 role may not open this stage's shell
(`STAGE_MIN_ROLE`: dev=developer, staging=tester, production=admin) · 4404
branch not found · 4409 container not running · 4500 exec failed.
The shell starts as `bash` with cwd `/mnt/user` inside the branch container;
`PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE/ODOO_STAGE` are exported.

## Management CLI

```bash
.venv/bin/python -m app.manage init
.venv/bin/python -m app.manage create-admin  --email a@b --password s [--role admin|developer|tester]
.venv/bin/python -m app.manage set-password  --email a@b --password s
.venv/bin/python -m app.manage reset-2fa     --email a@b
.venv/bin/python -m app.manage backup-all    [--kind manual|auto|update]
.venv/bin/python -m app.manage retention
.venv/bin/python -m app.manage doctor
```

## Interactive API docs

FastAPI serves its auto-generated OpenAPI UI at `/api/docs` (and
`/api/openapi.json`). Browser HTML form routes (POST /projects, /branches,
etc.) intentionally use `303` redirects with a `msg` query parameter.
