"""Control-plane domain, nginx mapping and localhost binding.

The control plane can be exposed through nginx on a real domain. Once HTTPS is
active, uvicorn is rebound to 127.0.0.1 so the raw IP:port endpoint is no longer
reachable from outside the host. Branch Odoo domains are handled separately.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Optional

from sqlmodel import Session, select

from app.config import settings
from app.errors import PlatformError
from app.models import Branch, Setting
from app.services import nginx

DOMAIN_KEY = "control_plane_domain"
ENV_PATH = settings.APP_DIR / ".env"


def get_domain(session: Session) -> str:
    row = session.get(Setting, DOMAIN_KEY)
    return (row.value or "").strip() if row else ""


def _store_domain(session: Session, domain: str) -> None:
    row = session.get(Setting, DOMAIN_KEY)
    if row is None:
        session.add(Setting(key=DOMAIN_KEY, value=domain))
    else:
        row.value = domain
        session.add(row)


def _clear_domain(session: Session) -> None:
    row = session.get(Setting, DOMAIN_KEY)
    if row is not None:
        session.delete(row)


def configured_host() -> str:
    """HOST currently written to .env (the value used on the next restart)."""
    try:
        for line in ENV_PATH.read_text().splitlines():
            if line.startswith("HOST="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return settings.HOST


def _write_host(host: str) -> None:
    if not ENV_PATH.exists():
        raise PlatformError("Platform .env file is missing; cannot change the bind address")
    try:
        lines = ENV_PATH.read_text().splitlines()
        output = []
        replaced = False
        for line in lines:
            if line.startswith("HOST="):
                if not replaced:
                    output.append(f"HOST={host}")
                    replaced = True
                continue
            output.append(line)
        if not replaced:
            output.append(f"HOST={host}")
        mode = ENV_PATH.stat().st_mode & 0o777
        tmp = ENV_PATH.with_name(".env.tmp")
        tmp.write_text("\n".join(output) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, ENV_PATH)
    except OSError as exc:
        raise PlatformError(f"Could not update HOST in .env: {exc}") from exc


def _schedule_restart() -> bool:
    """Restart the service outside its own cgroup after the response is sent."""
    if settings.MOCK_DOCKER:
        return False
    systemd_run = shutil.which("systemd-run")
    if not systemd_run:
        return False
    unit = f"odoo-paas-rebind-{time.time_ns()}"
    try:
        result = subprocess.run(
            [
                systemd_run,
                "--collect",
                f"--unit={unit}",
                "/bin/sh",
                "-c",
                "sleep 2; exec /bin/systemctl restart odoo-paas",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _domain_conflict(session: Session, domain: str) -> Optional[Branch]:
    return session.exec(select(Branch).where(Branch.domain == domain)).first()


def apply_domain(session: Session, raw_domain: str, email: str = "") -> dict:
    domain = nginx.normalize_domain(raw_domain)
    conflict = _domain_conflict(session, domain)
    if conflict is not None:
        raise PlatformError(
            f"Domain '{domain}' is already used by branch '{conflict.name}' "
            f"(project #{conflict.project_id})"
        )

    if settings.MOCK_DOCKER:
        _store_domain(session, domain)
        return {
            "message": "Domain saved (mock mode: nginx was not changed)",
            "https": False,
            "restart": False,
            "target": None,
        }

    message, https = nginx.control_plane_provision(domain, email)
    _store_domain(session, domain)

    desired_host = "127.0.0.1" if https else "0.0.0.0"
    needs_restart = configured_host() != desired_host or settings.HOST != desired_host
    if configured_host() != desired_host:
        _write_host(desired_host)
    restarted = _schedule_restart() if needs_restart else False
    if needs_restart and not restarted:
        message += " Restart odoo-paas to apply the bind address."

    scheme = "https" if https else "http"
    return {
        "message": message,
        "https": https,
        "restart": restarted,
        "target": f"{scheme}://{domain}/settings",
    }


def remove_domain(session: Session, fallback_host: str = "") -> dict:
    nginx.control_plane_remove()
    _clear_domain(session)
    if settings.MOCK_DOCKER:
        return {
            "message": "Domain removed (mock mode: nginx was not changed)",
            "restart": False,
            "target": None,
        }

    desired_host = "0.0.0.0"
    needs_restart = configured_host() != desired_host or settings.HOST != desired_host
    if configured_host() != desired_host:
        _write_host(desired_host)
    restarted = _schedule_restart() if needs_restart else False
    host = settings.PUBLIC_HOST or fallback_host or "127.0.0.1"
    target = f"http://{host}:{settings.PORT}/settings"
    message = "Domain removed"
    if needs_restart and not restarted:
        message += " Restart odoo-paas to restore the IP bind address."
    return {"message": message, "restart": restarted, "target": target}


def public_url(session: Session, request=None) -> str:
    domain = get_domain(session)
    if domain and nginx.control_plane_config_exists():
        scheme = "https" if nginx.control_plane_certificate_exists(domain) else "http"
        return f"{scheme}://{domain}"
    host = settings.PUBLIC_HOST or (request.url.hostname if request else "") or "127.0.0.1"
    return f"http://{host}:{settings.PORT}"


def status(session: Session, request) -> dict:
    domain = get_domain(session)
    https = bool(domain and nginx.control_plane_certificate_exists(domain))
    ip_url = (
        f"http://{settings.PUBLIC_HOST or request.url.hostname}:{settings.PORT}"
    )
    return {
        "domain": domain,
        "https": https,
        "config": nginx.control_plane_config_exists(),
        "bind_host": configured_host(),
        "runtime_host": settings.HOST,
        "ip_url": ip_url,
        "public_url": public_url(session, request),
        "acme_email": settings.ACME_EMAIL,
    }
