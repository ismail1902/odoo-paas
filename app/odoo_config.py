"""Odoo server configuration exposed in project settings.

Each project stores a JSON object in ``Project.odoo_config`` containing only
the options the user explicitly enabled. Options not present are left to Odoo
or to the platform defaults. The advanced field accepts any other server-side
CLI option supported by the project's Odoo series, except options the platform
manages itself.
"""
from __future__ import annotations

import json
import shlex
from typing import Any, Mapping

CONFIG_GROUPS = [
    {
        "title": "Performance",
        "options": [
            {
                "key": "workers",
                "flag": "--workers",
                "label": "HTTP workers",
                "type": "int",
                "min": 0,
                "max": 128,
                "placeholder": "0",
                "help": "Prefork worker processes; 0 disables prefork mode (threaded server).",
            },
            {
                "key": "max_cron_threads",
                "flag": "--max-cron-threads",
                "label": "Cron threads",
                "type": "int",
                "min": 0,
                "max": 64,
                "placeholder": "2 / 0",
                "help": "Concurrent cron threads. Platform default: 2 on production, 0 on other stages.",
            },
            {
                "key": "limit_time_cpu",
                "flag": "--limit-time-cpu",
                "label": "CPU limit per request (s)",
                "type": "int",
                "min": 0,
                "placeholder": "60",
                "help": "Maximum CPU seconds per request (Odoo default: 60).",
            },
            {
                "key": "limit_time_real",
                "flag": "--limit-time-real",
                "label": "Real time limit per request (s)",
                "type": "int",
                "min": 0,
                "placeholder": "120",
                "help": "Maximum wall-clock seconds per request (Odoo default: 120).",
            },
            {
                "key": "limit_memory_soft",
                "flag": "--limit-memory-soft",
                "label": "Soft memory limit (bytes)",
                "type": "int",
                "min": 0,
                "placeholder": "2147483648",
                "help": "Per-worker virtual memory before the worker is recycled (default 2 GiB).",
            },
            {
                "key": "limit_memory_hard",
                "flag": "--limit-memory-hard",
                "label": "Hard memory limit (bytes)",
                "type": "int",
                "min": 0,
                "placeholder": "2684354560",
                "help": "Per-worker virtual memory before allocations fail (default 2.5 GiB).",
            },
            {
                "key": "limit_request",
                "flag": "--limit-request",
                "label": "Requests per worker",
                "type": "int",
                "min": 0,
                "placeholder": "65536",
                "help": "Requests handled by a worker before it is recycled.",
            },
            {
                "key": "db_maxconn",
                "flag": "--db_maxconn",
                "label": "PostgreSQL max connections",
                "type": "int",
                "min": 1,
                "max": 1000,
                "placeholder": "64",
                "help": "Maximum physical PostgreSQL connections per Odoo process.",
            },
            {
                "key": "transient_age_limit",
                "flag": "--transient-age-limit",
                "label": "Transient record age (hours)",
                "type": "float",
                "min": 0,
                "placeholder": "1",
                "help": "Hours to keep wizard/transient records (Odoo default: 1).",
            },
            {
                "key": "limit_time_worker_cron",
                "flag": "--limit-time-worker-cron",
                "label": "Cron worker lifetime (s)",
                "type": "int",
                "min": 0,
                "placeholder": "0",
                "help": "Maximum lifetime of a cron thread/worker; 0 disables the limit.",
            },
            {
                "key": "limit_time_real_cron",
                "flag": "--limit-time-real-cron",
                "label": "Cron job real time limit (s)",
                "type": "int",
                "min": 0,
                "placeholder": "120",
                "help": "Maximum wall-clock seconds per cron job; 0 means no limit.",
            },
            {
                "key": "osv_memory_count_limit",
                "flag": "--osv-memory-count-limit",
                "label": "Transient model record limit",
                "type": "int",
                "min": 0,
                "placeholder": "0",
                "help": "Maximum records kept in virtual osv_memory tables; 0 means no limit.",
            },
        ],
    },
    {
        "title": "HTTP and security",
        "options": [
            {
                "key": "proxy_mode",
                "flag": "--proxy-mode",
                "label": "Proxy mode",
                "type": "flag",
                "help": "Trust X-Forwarded-* headers. Only enable behind a trusted reverse proxy.",
            },
            {
                "key": "x_sendfile",
                "flag": "--x-sendfile",
                "label": "X-Sendfile",
                "type": "flag",
                "help": "Delegate large file delivery to Apache/nginx via X-Sendfile/X-Accel-Redirect.",
            },
            {
                "key": "no_database_list",
                "flag": "--no-database-list",
                "label": "Hide database list",
                "type": "flag",
                "help": "Disable the database list and database manager. Recommended for exposed instances.",
            },
            {
                "key": "unaccent",
                "flag": "--unaccent",
                "label": "Enable unaccent",
                "type": "flag",
                "help": "Try to enable the PostgreSQL unaccent extension when creating databases.",
            },
        ],
    },
    {
        "title": "Logging",
        "options": [
            {
                "key": "log_level",
                "flag": "--log-level",
                "label": "Log level",
                "type": "select",
                "default": "info",
                "choices": [
                    "info",
                    "warn",
                    "error",
                    "critical",
                    "debug",
                    "debug_sql",
                    "debug_rpc",
                    "notset",
                ],
                "help": "Server log verbosity. Platform default: info.",
            },
            {
                "key": "log_handler",
                "flag": "--log-handler",
                "label": "Log handler",
                "type": "text",
                "placeholder": "odoo.addons.mail:DEBUG",
                "help": "One or more MODULE:LEVEL handlers, comma-separated (e.g. odoo.addons.mail:DEBUG).",
            },
            {
                "key": "log_web",
                "flag": "--log-web",
                "label": "Log web requests",
                "type": "flag",
                "help": "Shortcut for --log-handler=odoo.http:DEBUG.",
            },
            {
                "key": "log_sql",
                "flag": "--log-sql",
                "label": "Log SQL queries",
                "type": "flag",
                "help": "Shortcut for --log-handler=odoo.sql_db:DEBUG.",
            },
        ],
    },
    {
        "title": "Email (SMTP)",
        "options": [
            {
                "key": "smtp_server",
                "flag": "--smtp",
                "label": "SMTP server",
                "type": "text",
                "placeholder": "odoo-paas-mailpit",
                "help": "SMTP host. The platform's Mailpit is odoo-paas-mailpit (port 1025).",
            },
            {
                "key": "smtp_port",
                "flag": "--smtp-port",
                "label": "SMTP port",
                "type": "int",
                "min": 1,
                "max": 65535,
                "placeholder": "1025",
                "help": "SMTP port. Mailpit listens on 1025.",
            },
            {
                "key": "smtp_user",
                "flag": "--smtp-user",
                "label": "SMTP user",
                "type": "text",
                "placeholder": "",
                "help": "Optional SMTP username.",
            },
            {
                "key": "smtp_password",
                "flag": "--smtp-password",
                "label": "SMTP password",
                "type": "password",
                "placeholder": "",
                "help": "Stored in the platform database in plain text; use a dedicated account.",
            },
            {
                "key": "smtp_ssl",
                "flag": "--smtp-ssl",
                "label": "SMTP SSL / STARTTLS",
                "type": "flag",
                "help": "Encrypt SMTP connections with SSL/STARTTLS.",
            },
            {
                "key": "email_from",
                "flag": "--email-from",
                "label": "Default From address",
                "type": "text",
                "placeholder": "odoo@example.com",
                "help": "Default sender address for outgoing email.",
            },
            {
                "key": "from_filter",
                "flag": "--from-filter",
                "label": "From filter",
                "type": "text",
                "placeholder": "",
                "help": "Only use this SMTP configuration for the given sender address/domain.",
            },
        ],
    },
    {
        "title": "Developer",
        "options": [
            {
                "key": "dev",
                "flag": "--dev",
                "label": "Developer features",
                "type": "select",
                "choices": ["all", "qweb", "xml", "reload"],
                "help": "Enable Odoo developer features. Avoid 'all' on production branches.",
            },
        ],
    },
]

OPTIONS = {
    option["key"]: option
    for group in CONFIG_GROUPS
    for option in group["options"]
}

# Platform-managed options and curated options (which have dedicated fields)
# must not be overridden from the advanced text area.
_RESERVED_FLAGS = {
    "--addons-path",
    "--config",
    "--data-dir",
    "--database",
    "--db-filter",
    "--db_host",
    "--db_password",
    "--db_port",
    "--db_replica_host",
    "--db_replica_port",
    "--db-system",
    "--db-template",
    "--db_user",
    "--gevent-port",
    "--help",
    "--http-interface",
    "--http-port",
    "--import-partial",
    "--init",
    "--log-config",
    "--logfile",
    "--no-http",
    "--pg_path",
    "--pidfile",
    "--pre-upgrade-scripts",
    "--reinit",
    "--save",
    "--screenshots",
    "--screencasts",
    "--shell-interface",
    "--skip-auto-install",
    "--stop",
    "--stop-after-init",
    "--test-enable",
    "--test-file",
    "--test-tags",
    "--update",
    "--upgrade-path",
    "--version",
    "--with-demo",
    "--without-demo",
} | {option["flag"] for option in OPTIONS.values()}


def _coerce(option: dict, value: Any) -> Any:
    kind = option["type"]
    if kind == "flag":
        return True if value in (True, 1, "1", "on", "true", "yes") else None
    if kind in ("int", "float"):
        number = int(value) if kind == "int" else float(value)
        if "min" in option and number < option["min"]:
            raise ValueError(f"must be at least {option['min']}")
        if "max" in option and number > option["max"]:
            raise ValueError(f"must be at most {option['max']}")
        return number
    text = str(value).strip()
    if not text:
        return None
    if kind == "select":
        if text not in option["choices"]:
            raise ValueError(f"must be one of: {', '.join(option['choices'])}")
        return text
    if len(text) > 1000:
        raise ValueError("is too long")
    return text


def parse_config(raw: Any) -> dict:
    """Return the validated configuration stored on a project."""
    if isinstance(raw, Mapping):
        data = dict(raw)
    else:
        try:
            data = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
    config = {}
    for key, value in data.items():
        if key == "extra":
            if isinstance(value, str) and value.strip():
                config["extra"] = value.strip()
            continue
        option = OPTIONS.get(key)
        if option is None:
            continue
        try:
            coerced = _coerce(option, value)
        except (TypeError, ValueError):
            continue
        if coerced is not None:
            config[key] = coerced
    return config


def dump_config(config: Mapping[str, Any]) -> str:
    return json.dumps(dict(config), sort_keys=True, separators=(",", ":"))


def config_from_form(form: Mapping[str, Any]) -> tuple[dict, str]:
    """Build a config from project-settings form fields.

    Returns ``(config, error)``; ``error`` is empty on success.
    """
    config = {}
    errors = []
    for key, option in OPTIONS.items():
        if not form.get(f"enabled_{key}"):
            continue
        if option["type"] == "flag":
            config[key] = True
            continue
        raw = str(form.get(f"value_{key}", "") or "").strip()
        if not raw:
            continue
        try:
            value = _coerce(option, raw)
        except (TypeError, ValueError) as exc:
            errors.append(f"{option['label']}: {exc}")
            continue
        if value is not None:
            config[key] = value
    extra = str(form.get("extra", "") or "").strip()
    if extra:
        try:
            _parse_extra(extra)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            config["extra"] = extra
    if errors:
        return {}, "; ".join(errors)
    return config, ""


def cli_args(config: Mapping[str, Any]) -> list:
    """Arguments for every enabled option except the two handled by the caller."""
    args = []
    for key, option in OPTIONS.items():
        if key in ("log_level", "max_cron_threads") or key not in config:
            continue
        value = config[key]
        if option["type"] == "flag":
            if value:
                args.append(option["flag"])
        elif key == "log_handler":
            for handler in str(value).split(","):
                if handler.strip():
                    args.extend([option["flag"], handler.strip()])
        elif value not in (None, ""):
            args.extend([option["flag"], str(value)])
    extra = config.get("extra")
    if extra:
        args.extend(_parse_extra(str(extra)))
    return args


def _parse_extra(raw: str) -> list:
    if len(raw) > 2000:
        raise ValueError("Advanced options: too long (maximum 2000 characters)")
    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError as exc:
        raise ValueError(f"Advanced options: {exc}") from exc
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--" or not token.startswith("--"):
            raise ValueError(
                f"Advanced options: expected a long option starting with '--', got {token!r}"
            )
        name = token.split("=", 1)[0]
        if name in _RESERVED_FLAGS:
            raise ValueError(
                f"Advanced options: {name} is managed by the platform or has a dedicated field"
            )
        if "=" not in token and index + 1 < len(tokens) and not tokens[index + 1].startswith("--"):
            index += 2
        else:
            index += 1
    return tokens
