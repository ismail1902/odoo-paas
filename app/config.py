"""Platform configuration.

Single source of truth for paths, ports, Docker names and feature flags.
Values are read from environment variables (optionally via a .env file at the
repository root). No other module should read os.environ directly.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file(APP_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class Settings:
    """Resolved platform settings (instantiated once at import)."""

    def __init__(self) -> None:
        self.APP_NAME = os.environ.get("APP_NAME", "Odoo PaaS")
        self.APP_DIR = APP_DIR
        self.DATA_DIR = Path(os.environ.get("DATA_DIR", APP_DIR / "var")).resolve()
        self.BUILD_ROOT = Path(os.environ.get("BUILD_ROOT", self.DATA_DIR / "builds")).resolve()
        self.BACKUP_ROOT = Path(os.environ.get("BACKUP_ROOT", self.DATA_DIR / "backups")).resolve()
        self.LOG_ROOT = Path(os.environ.get("LOG_ROOT", self.DATA_DIR / "logs")).resolve()
        self.STATE_DB = Path(os.environ.get("STATE_DB", self.DATA_DIR / "state.db")).resolve()

        self.SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-change-me")
        self.ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")
        self.ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
        self.SESSION_MAX_AGE = _int("SESSION_MAX_AGE", 7 * 24 * 3600)

        self.HOST = os.environ.get("HOST", "0.0.0.0")
        self.PORT = _int("PORT", 8000)
        self.PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "")

        self.PORT_RANGE_START = _int("PORT_RANGE_START", 9000)
        self.PORT_RANGE_END = _int("PORT_RANGE_END", 9990)

        self.DOCKER_NETWORK = os.environ.get("DOCKER_NETWORK", "odoo-paas")
        self.PG_CONTAINER = os.environ.get("PG_CONTAINER", "odoo-paas-pg")
        self.PG_USER = os.environ.get("PG_USER", "odoo")
        self.PG_PASSWORD = os.environ.get("PG_PASSWORD", "odoo-paas")
        self.MAILPIT_CONTAINER = os.environ.get("MAILPIT_CONTAINER", "odoo-paas-mailpit")
        self.MAILPIT_PORT = _int("MAILPIT_PORT", 8025)
        self.BASE_IMAGE = os.environ.get("BASE_IMAGE", "odoo-paas/base:17.0")
        self.ODOO_VERSION = os.environ.get("ODOO_VERSION", "17.0")
        # Versions offered in mock mode / when no images are built yet.
        self.ODOO_VERSIONS = [
            v.strip()
            for v in os.environ.get("ODOO_VERSIONS", "17.0,18.0,19.0,20.0").split(",")
            if v.strip()
        ]

        # Builds are simulated on macOS (the pipeline targets Linux hosts).
        self.MOCK_DOCKER = _bool("MOCK_DOCKER", sys.platform == "darwin")
        self.WORKER_ENABLED = _bool("WORKER_ENABLED", True)

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.STATE_DB}"

    def ensure_dirs(self) -> None:
        for path in (self.DATA_DIR, self.BUILD_ROOT, self.BACKUP_ROOT, self.LOG_ROOT):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
