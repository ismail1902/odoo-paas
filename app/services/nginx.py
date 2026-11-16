"""Per-branch nginx reverse proxy and Let's Encrypt certificates.

The proxy/websocket/SSL blocks follow Odoo's official Nginx HTTPS sample:
https://www.odoo.com/documentation/20.0/administration/on_premise/deploy.html#https
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from app.config import settings
from app.errors import PlatformError

CONFIG_DIR = Path("/etc/nginx/conf.d")
WEBROOT = Path("/var/www/letsencrypt")
DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


def available() -> bool:
    return shutil.which("nginx") is not None


def certbot_available() -> bool:
    return shutil.which("certbot") is not None


def normalize_domain(value: str) -> str:
    domain = (value or "").strip().lower().rstrip(".")
    if not domain or not DOMAIN_RE.match(domain):
        raise PlatformError(
            "Enter a valid domain name without scheme or path, e.g. app.example.com"
        )
    return domain


def certificate_exists(domain: str) -> bool:
    base = Path("/etc/letsencrypt/live") / domain
    return (base / "fullchain.pem").exists() and (base / "privkey.pem").exists()


def config_path(branch) -> Path:
    return CONFIG_DIR / f"odoo-paas-branch-{branch.id}.conf"


_VERSION_CACHE = None


def _nginx_supports_cookie_flags() -> bool:
    global _VERSION_CACHE
    if _VERSION_CACHE is None:
        try:
            result = subprocess.run(
                ["nginx", "-v"], capture_output=True, text=True
            )
            match = re.search(r"nginx/(\d+)\.(\d+)\.(\d+)", result.stderr or "")
            _VERSION_CACHE = (
                tuple(int(part) for part in match.groups()) if match else (0, 0, 0)
            )
        except OSError:
            _VERSION_CACHE = (0, 0, 0)
    return _VERSION_CACHE >= (1, 19, 8)


def _proxy_locations(branch, secure_cookies: bool) -> str:
    """Locations based on Odoo's official Nginx reverse-proxy sample."""
    odoo = f"odoo_http_{branch.id}"
    gevent = f"odoo_gevent_{branch.id}"
    upgrade = f"$connection_upgrade_{branch.id}"
    cookie_flags = (
        "        proxy_cookie_flags session_id samesite=lax secure;\n"
        if secure_cookies and _nginx_supports_cookie_flags()
        else ""
    )
    headers = (
        "        proxy_set_header X-Forwarded-Host $http_host;\n"
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "        proxy_set_header X-Forwarded-Proto $scheme;\n"
        "        proxy_set_header X-Real-IP $remote_addr;\n"
        f"{cookie_flags}"
    )

    def websocket(path: str) -> str:
        return f"""
    location {path} {{
        proxy_pass http://{gevent};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection {upgrade};
{headers}        proxy_buffering off;
    }}
"""

    return (
        websocket("/websocket")
        + websocket("/longpolling")
        + f"""
    location / {{
        proxy_pass http://{odoo};
        proxy_redirect off;
{headers}    }}
"""
    )


def _render(branch, domain: str, ssl: bool) -> str:
    if not getattr(branch, "gevent_port", 0):
        raise PlatformError("This branch has no gevent port allocated")
    upstreams = (
        f"upstream odoo_http_{branch.id} {{ server 127.0.0.1:{branch.port}; }}\n"
        f"upstream odoo_gevent_{branch.id} {{ "
        f"server 127.0.0.1:{branch.gevent_port}; }}\n"
    )
    upgrade_map = f"""map $http_upgrade $connection_upgrade_{branch.id} {{
    default upgrade;
    ''      close;
}}
"""
    challenge = (
        "    location /.well-known/acme-challenge/ {\n"
        "        root /var/www/letsencrypt;\n"
        "    }\n"
    )
    common = f"""
    server_name {domain};
    client_max_body_size 200m;
    proxy_read_timeout 720s;
    proxy_connect_timeout 720s;
    proxy_send_timeout 720s;
"""
    http_body = (
        f"{challenge}\n    location / {{ return 301 https://$host$request_uri; }}"
        if ssl
        else challenge + _proxy_locations(branch, secure_cookies=False)
    )
    http_block = f"""
server {{
    listen 80;
{common}{http_body}
}}
"""
    if not ssl:
        return upstreams + upgrade_map + http_block
    tls_block = f"""
server {{
    listen 443 ssl;
{common}    ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;
    ssl_session_timeout 30m;
    ssl_protocols TLSv1.2;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384;
    ssl_prefer_server_ciphers off;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    access_log /var/log/nginx/odoo-paas-{branch.id}.access.log;
    error_log /var/log/nginx/odoo-paas-{branch.id}.error.log;
    gzip_types text/css text/scss text/plain text/xml application/xml application/json application/javascript;
    gzip on;
{_proxy_locations(branch, secure_cookies=True)}
}}
"""
    return upstreams + upgrade_map + http_block + tls_block


def _write_and_reload(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    WEBROOT.mkdir(parents=True, exist_ok=True)
    old = path.read_text() if path.exists() else None
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)
    test = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
    if test.returncode != 0:
        if old is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(old)
        raise PlatformError(
            "nginx config test failed: " + (test.stderr or test.stdout).strip()[-800:]
        )
    reload = subprocess.run(
        ["systemctl", "reload", "nginx"], capture_output=True, text=True
    )
    if reload.returncode != 0:
        raise PlatformError(
            "nginx reload failed: " + (reload.stderr or reload.stdout).strip()[-500:]
        )


def provision(branch, domain: str, email: str = "") -> str:
    """Write the HTTP config, obtain a certificate, then enable HTTPS."""
    if settings.MOCK_DOCKER:
        return "mock mode: nginx config not written"
    if not available():
        raise PlatformError("nginx is not installed on this host")
    if not getattr(branch, "gevent_port", 0):
        raise PlatformError("This branch has no gevent port allocated")
    WEBROOT.mkdir(parents=True, exist_ok=True)
    path = config_path(branch)
    _write_and_reload(path, _render(branch, domain, ssl=False))
    if certificate_exists(domain):
        _write_and_reload(path, _render(branch, domain, ssl=True))
        return "HTTPS is enabled"
    if not certbot_available():
        return "HTTP is configured; certbot is not installed, so HTTPS was skipped"
    command = [
        "certbot",
        "certonly",
        "--webroot",
        "-w",
        str(WEBROOT),
        "-d",
        domain,
        "--non-interactive",
        "--agree-tos",
        "--deploy-hook",
        "systemctl reload nginx",
    ]
    if (email or "").strip():
        command += ["--email", email.strip()]
    else:
        command += ["--register-unsafely-without-email"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return "HTTP is configured; certbot timed out, so HTTPS is pending"
    if result.returncode != 0:
        lines = (result.stderr or result.stdout).strip().splitlines()
        detail = lines[-1] if lines else "certbot failed"
        return f"HTTP is configured; HTTPS pending: {detail[:300]}"
    _write_and_reload(path, _render(branch, domain, ssl=True))
    return "HTTPS is enabled"


def remove(branch) -> None:
    path = config_path(branch)
    if not path.exists():
        return
    if settings.MOCK_DOCKER or not available():
        path.unlink(missing_ok=True)
        return
    old = path.read_text()
    path.unlink()
    test = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
    if test.returncode != 0:
        path.write_text(old)
        raise PlatformError(
            "nginx config test failed: " + (test.stderr or test.stdout).strip()[-800:]
        )
    subprocess.run(["systemctl", "reload", "nginx"], capture_output=True, text=True)
