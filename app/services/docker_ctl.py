"""Docker orchestration. All Docker access goes through this module.

Topology
--------
- One shared Postgres container (``PG_CONTAINER``) hosts one database per
  branch. It is only published on 127.0.0.1 by install.sh.
- One shared bridge network (``DOCKER_NETWORK``). Build containers join it and
  reach Postgres by container name.
- Each branch is a container running the base image (``BASE_IMAGE``) with:
    {branch_dir}/repo      -> /mnt/user       user repository checkout
    {branch_dir}/filestore -> /mnt/filestore  Odoo --data-dir
    {branch_dir}/venv      -> /mnt/venv       per-branch pip venv
- The base image ships Odoo core at /opt/odoo and a venv at /opt/venv.
- MOCK_DOCKER=true short-circuits every operation (local UI development).
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Iterator, Optional

from app import odoo_config
from app.config import settings
from app.errors import BuildError

# uid/gid of the 'odoo' user inside the base image (see docker/odoo-base/Dockerfile)
CONTAINER_UID = 1000
CONTAINER_GID = 1000

_client = None
_client_error: Optional[str] = None


# --- Client ------------------------------------------------------------------


def client():
    global _client, _client_error
    if _client is None and not _client_error:
        if settings.MOCK_DOCKER:
            _client_error = "mock mode"
        else:
            try:
                import docker  # type: ignore

                _client = docker.from_env()
                _client.ping()
            except Exception as exc:  # noqa: BLE001 - surfaced as BuildError
                _client_error = str(exc)
    if _client_error:
        raise BuildError(f"Docker unavailable: {_client_error}")
    return _client


def available() -> bool:
    if settings.MOCK_DOCKER:
        return True
    try:
        client()
        return True
    except BuildError:
        return False


# --- Names and paths -----------------------------------------------------------


def container_name(branch) -> str:
    return f"odoo-paas-branch-{branch.id}"


def branch_dir(branch) -> Path:
    return settings.BUILD_ROOT / f"branch-{branch.id}"


def repo_dir(branch) -> Path:
    return branch_dir(branch) / "repo"


def venv_dir(branch) -> Path:
    return branch_dir(branch) / "venv"


def filestore_dir(branch) -> Path:
    return branch_dir(branch) / "filestore"


def enterprise_dir(branch) -> Path:
    return branch_dir(branch) / "enterprise"


def ensure_branch_dirs(branch) -> None:
    for path in (repo_dir(branch), venv_dir(branch), filestore_dir(branch), enterprise_dir(branch)):
        path.mkdir(parents=True, exist_ok=True)
        _make_container_writable(path)


# --- Odoo version images -------------------------------------------------------


def _version_key(version: str) -> tuple:
    return tuple(int(part) for part in version.split(".") if part.isdigit()) or (0,)


def image_name(version: str) -> str:
    repo = settings.BASE_IMAGE.rsplit(":", 1)[0]
    return f"{repo}:{version or settings.ODOO_VERSION}"


def image_for(branch) -> str:
    return image_name(getattr(branch, "odoo_version", "") or settings.ODOO_VERSION)


def image_exists(version: str) -> bool:
    if settings.MOCK_DOCKER:
        return True
    try:
        client().images.get(image_name(version))
        return True
    except Exception:  # noqa: BLE001 - docker.errors.ImageNotFound
        return False


def list_base_versions() -> list:
    """Tags of base images built on this host, newest first."""
    if settings.MOCK_DOCKER:
        return sorted(settings.ODOO_VERSIONS, key=_version_key, reverse=True)
    try:
        repo = settings.BASE_IMAGE.rsplit(":", 1)[0]
        tags = {
            image.tags[0].rsplit(":", 1)[1]
            for image in client().images.list(name=repo)
            if image.tags
        }
        versions = sorted((t for t in tags if t and t != "latest"), key=_version_key, reverse=True)
        return versions or [settings.ODOO_VERSION]
    except Exception:  # noqa: BLE001 - docker unavailable
        return [settings.ODOO_VERSION]


def _make_container_writable(path: Path) -> None:
    """Let the container user (uid 1000 'odoo') write to a host directory."""
    try:
        os.chmod(path, 0o775)
        os.chown(path, CONTAINER_UID, CONTAINER_GID)
    except (OSError, AttributeError):
        pass  # non-POSIX dev machines (e.g. macOS)


# --- Container definitions -----------------------------------------------------


def _volumes(branch) -> dict:
    return {
        str(repo_dir(branch)): {"bind": "/mnt/user", "mode": "rw"},
        str(filestore_dir(branch)): {"bind": "/mnt/filestore", "mode": "rw"},
        str(venv_dir(branch)): {"bind": "/mnt/venv", "mode": "rw"},
        str(enterprise_dir(branch)): {"bind": "/mnt/enterprise", "mode": "rw"},
    }


def _env(branch) -> dict:
    return {
        "PGHOST": settings.PG_CONTAINER,
        "PGPORT": "5432",
        "PGUSER": settings.PG_USER,
        "PGPASSWORD": settings.PG_PASSWORD,
        "PGDATABASE": branch.db_name,
        "ODOO_STAGE": branch.stage,
        "ODOO_VERSION": settings.ODOO_VERSION,
        "HOME": "/tmp",
    }


def _base_odoo_argv(branch, enterprise: bool = False) -> list:
    addons = "/opt/odoo/addons,/mnt/user"
    if enterprise:
        addons += ",/mnt/enterprise"
    return [
        "/mnt/venv/bin/python",
        "/opt/odoo/odoo-bin",
        "-d",
        branch.db_name,
        "--addons-path",
        addons,
        "--data-dir",
        "/mnt/filestore",
    ]


def server_command(branch, config=None, enterprise: bool = False) -> list:
    conf = odoo_config.parse_config(config)
    log_level = conf.get("log_level", "info")
    max_cron = conf.get("max_cron_threads")
    if max_cron is None:
        max_cron = 2 if branch.stage == "production" else 0
    return _base_odoo_argv(branch, enterprise) + [
        "--db-filter",
        f"^{branch.db_name}$",
        # Odoo >= 19 binds 127.0.0.1 by default; the published port needs 0.0.0.0.
        "--http-interface",
        "0.0.0.0",
        "--http-port",
        "8069",
        "--log-level",
        str(log_level),
        "--max-cron-threads",
        str(max_cron),
    ] + odoo_config.cli_args(conf)


def task_command(
    branch,
    *,
    install: str = "",
    update: bool = False,
    demo: bool = False,
    tests: bool = False,
    test_tags: str = "",
    enterprise: bool = False,
) -> list:
    """One-off Odoo command (DB init/update/tests)."""
    cmd = _base_odoo_argv(branch, enterprise) + ["--no-http", "--stop-after-init"]
    cmd += ["--log-level", "test" if tests else "info"]
    if install:
        cmd += ["-i", install]
    if update:
        cmd += ["-u", "all"]
    if not demo:
        cmd += ["--without-demo=all"]
    if tests:
        cmd += ["--test-enable"]
        if test_tags:
            cmd += ["--test-tags", test_tags]
    return cmd


def ensure_network() -> None:
    if settings.MOCK_DOCKER:
        return
    c = client()
    try:
        c.networks.get(settings.DOCKER_NETWORK)
    except Exception:  # noqa: BLE001 - docker.errors.NotFound
        c.networks.create(settings.DOCKER_NETWORK, driver="bridge")


# --- Runtime containers ----------------------------------------------------------


def start_server_container(branch, config=None, enterprise: bool = False) -> None:
    """(Re)create and start the detached Odoo HTTP server for a branch."""
    ensure_branch_dirs(branch)
    if settings.MOCK_DOCKER:
        return
    c = client()
    name = container_name(branch)
    try:
        existing = c.containers.get(name)
        existing.remove(force=True)
    except Exception:  # noqa: BLE001 - not found
        pass
    c.containers.run(
        image_for(branch),
        server_command(branch, config, enterprise),
        name=name,
        detach=True,
        remove=False,
        network=settings.DOCKER_NETWORK,
        volumes=_volumes(branch),
        environment=_env(branch),
        ports={"8069/tcp": branch.port},
        user="odoo",
        working_dir="/mnt/user",
        restart_policy={"Name": "unless-stopped"},
    )


def stop_container(name: str, timeout: int = 20) -> None:
    if settings.MOCK_DOCKER:
        return
    try:
        client().containers.get(name).stop(timeout=timeout)
    except Exception:  # noqa: BLE001 - missing or already stopped
        pass


def remove_container(name: str) -> None:
    if settings.MOCK_DOCKER:
        return
    try:
        client().containers.get(name).remove(force=True)
    except Exception:  # noqa: BLE001
        pass


def container_state(name: str) -> str:
    if settings.MOCK_DOCKER:
        return "missing"
    try:
        return client().containers.get(name).status
    except Exception:  # noqa: BLE001
        return "missing"


def run_oneoff(branch, command: list, log: Callable[[str], None], timeout: int = 3600) -> int:
    """Run a one-off task container; stream its output through ``log``.

    Returns the exit code. Raises BuildError on timeout.
    """
    ensure_branch_dirs(branch)
    if settings.MOCK_DOCKER:
        log(f"[mock] docker run {' '.join(command)}")
        log("[mock] task finished with exit code 0")
        return 0
    c = client()
    name = f"{container_name(branch)}-task-{int(time.time()) % 100000}"
    container = c.containers.run(
        image_for(branch),
        command,
        name=name,
        detach=True,
        network=settings.DOCKER_NETWORK,
        volumes=_volumes(branch),
        environment=_env(branch),
        user="odoo",
        working_dir="/mnt/user",
    )
    started = time.time()
    try:
        for chunk in container.logs(stream=True, follow=True):
            for line in chunk.decode(errors="replace").splitlines():
                log(line)
            if time.time() - started > timeout:
                container.kill()
                raise BuildError(f"task timed out after {timeout}s")
        result = container.wait()
        return int(result.get("StatusCode", 1))
    finally:
        try:
            container.remove(force=True)
        except Exception:  # noqa: BLE001
            pass


def ensure_venv(branch, log: Callable[[str], None]) -> int:
    """Create the per-branch venv and install requirements.txt files."""
    script = (
        "set -e; "
        "want=$(python3 -c 'import sys; print(\"%d.%d\" % sys.version_info[:2])'); "
        "have=\"\"; "
        "if [ -x /mnt/venv/bin/python ]; then "
        "  have=$(/mnt/venv/bin/python -c 'import sys; print(\"%d.%d\" % sys.version_info[:2])' 2>/dev/null || true); "
        "fi; "
        "if [ \"$have\" != \"$want\" ]; then "
        "  find /mnt/venv -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true; "
        "  python3 -m venv --system-site-packages /mnt/venv; "
        "  echo \"venv created (python $want)\"; "
        "fi; "
        "found=0; "
        "for req in requirements.txt $(find . -mindepth 2 -maxdepth 2 -name requirements.txt 2>/dev/null) $(find /mnt/enterprise -mindepth 1 -maxdepth 2 -name requirements.txt 2>/dev/null); do "
        "  if [ -f \"$req\" ]; then found=1; /mnt/venv/bin/pip install --quiet -r \"$req\"; fi; "
        "done; "
        "if [ \"$found\" = \"1\" ]; then echo 'requirements installed'; else echo 'no requirements.txt found'; fi"
    )
    return run_oneoff(branch, ["bash", "-lc", script], log, timeout=1800)


def stream_container_logs(branch, tail: int = 200) -> Iterator[str]:
    if settings.MOCK_DOCKER:
        yield "[mock] no container logs\n"
        return
    try:
        container = client().containers.get(container_name(branch))
    except Exception:  # noqa: BLE001
        yield "(container not running)\n"
        return
    for chunk in container.logs(stream=True, follow=True, tail=tail):
        yield chunk.decode(errors="replace")


def container_stats(branch) -> Optional[dict]:
    if settings.MOCK_DOCKER:
        return {
            "cpu_percent": 0.0,
            "mem_used": 0,
            "mem_limit": 0,
            "mem_percent": 0.0,
            "pids": 0,
            "net_rx": 0,
            "net_tx": 0,
            "blk_read": 0,
            "blk_write": 0,
        }
    try:
        container = client().containers.get(container_name(branch))
        raw = container.stats(stream=False)
    except Exception:  # noqa: BLE001
        return None
    try:
        cpu = raw["cpu_stats"]
        precpu = raw["precpu_stats"]
        cpu_delta = cpu["cpu_usage"]["total_usage"] - precpu["cpu_usage"]["total_usage"]
        system_delta = cpu.get("system_cpu_usage", 0) - precpu.get("system_cpu_usage", 0)
        num_cpus = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage", [1]))
        percent = (cpu_delta / system_delta) * num_cpus * 100.0 if system_delta else 0.0
        mem = raw["memory_stats"]
        used = max(0, mem.get("usage", 0) - mem.get("stats", {}).get("cache", 0))
        limit = int(mem.get("limit", 0))
        networks = raw.get("networks") or {}
        blkio = raw.get("blkio_stats", {}).get("io_service_bytes_recursive") or []
        return {
            "cpu_percent": round(percent, 1),
            "mem_used": int(used),
            "mem_limit": limit,
            "mem_percent": round(used / limit * 100.0, 1) if limit else 0.0,
            "pids": int((raw.get("pids_stats") or {}).get("current", 0)),
            "net_rx": sum(int(net.get("rx_bytes", 0)) for net in networks.values()),
            "net_tx": sum(int(net.get("tx_bytes", 0)) for net in networks.values()),
            "blk_read": sum(
                int(item.get("value", 0))
                for item in blkio
                if str(item.get("op", "")).lower() == "read"
            ),
            "blk_write": sum(
                int(item.get("value", 0))
                for item in blkio
                if str(item.get("op", "")).lower() == "write"
            ),
        }
    except (KeyError, TypeError):
        return None


# --- Postgres helpers (host-side `docker exec`) ----------------------------------


def _docker_exec_pg(args: list, input_bytes: Optional[bytes] = None) -> subprocess.CompletedProcess:
    cmd = ["docker", "exec"]
    if input_bytes is not None:
        cmd.append("-i")
    cmd += [settings.PG_CONTAINER, *args]
    return subprocess.run(cmd, input=input_bytes, capture_output=True)


def database_exists(db_name: str) -> bool:
    if settings.MOCK_DOCKER:
        return False
    result = _docker_exec_pg(
        [
            "psql",
            "-U",
            settings.PG_USER,
            "-d",
            "postgres",
            "-tAc",
            f"SELECT 1 FROM pg_database WHERE datname = '{db_name}'",
        ]
    )
    return result.stdout.strip() == b"1"


def ensure_database(db_name: str) -> bool:
    """Create the database if missing. Returns True when newly created."""
    if settings.MOCK_DOCKER:
        return True
    if database_exists(db_name):
        return False
    result = _docker_exec_pg(["createdb", "-U", settings.PG_USER, db_name])
    if result.returncode != 0:
        raise BuildError(f"createdb failed: {result.stderr.decode(errors='replace')}")
    return True


def database_initialized(db_name: str) -> bool:
    """True when Odoo has initialized the database (ir_module_module exists)."""
    if settings.MOCK_DOCKER:
        return True
    result = _docker_exec_pg(
        [
            "psql",
            "-U",
            settings.PG_USER,
            "-d",
            db_name,
            "-tAc",
            "select 1 from information_schema.tables "
            "where table_schema='public' and table_name='ir_module_module'",
        ]
    )
    return result.returncode == 0 and result.stdout.strip() == b"1"


def installed_modules(db_name: str, modules: list) -> set:
    """Subset of ``modules`` already installed in the database."""
    if settings.MOCK_DOCKER:
        return set(modules)
    if not modules:
        return set()
    quoted = ",".join("'" + m.replace("'", "''") + "'" for m in modules)
    result = _docker_exec_pg(
        [
            "psql",
            "-U",
            settings.PG_USER,
            "-d",
            db_name,
            "-tAc",
            "select name from ir_module_module "
            f"where state='installed' and name in ({quoted})",
        ]
    )
    if result.returncode != 0:
        return set()
    return {
        line.strip()
        for line in result.stdout.decode(errors="replace").splitlines()
        if line.strip()
    }


def drop_database(db_name: str) -> None:
    if settings.MOCK_DOCKER:
        return
    _docker_exec_pg(
        [
            "psql",
            "-U",
            settings.PG_USER,
            "-d",
            "postgres",
            "-c",
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{db_name}' AND pid <> pg_backend_pid()",
        ]
    )
    result = _docker_exec_pg(["dropdb", "--if-exists", "-U", settings.PG_USER, db_name])
    if result.returncode != 0:
        raise BuildError(f"dropdb failed: {result.stderr.decode(errors='replace')}")


def dump_database(db_name: str, dest: Path) -> None:
    if settings.MOCK_DOCKER:
        dest.write_bytes(b"[mock dump]")
        return
    with dest.open("wb") as fh:
        result = subprocess.run(
            [
                "docker",
                "exec",
                settings.PG_CONTAINER,
                "pg_dump",
                "-U",
                settings.PG_USER,
                "-Fc",
                db_name,
            ],
            stdout=fh,
            stderr=subprocess.PIPE,
        )
    if result.returncode != 0:
        dest.unlink(missing_ok=True)
        raise BuildError(f"pg_dump failed: {result.stderr.decode(errors='replace')}")


def restore_database(db_name: str, src: Path) -> None:
    if settings.MOCK_DOCKER:
        return
    drop_database(db_name)
    ensure_database(db_name)
    with src.open("rb") as fh:
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                settings.PG_CONTAINER,
                "pg_restore",
                "-U",
                settings.PG_USER,
                "-d",
                db_name,
                "--no-owner",
                "--role",
                settings.PG_USER,
            ],
            stdin=fh,
            capture_output=True,
        )
    if result.returncode != 0:
        raise BuildError(f"pg_restore failed: {result.stderr.decode(errors='replace')}")


# --- Interactive terminal (web shell) ---------------------------------------------


class Terminal:
    """Interactive TTY inside a running branch container (web shell)."""

    def __init__(self, branch):
        if settings.MOCK_DOCKER:
            raise BuildError("Web shell is unavailable in mock mode")
        c = client()
        container = c.containers.get(container_name(branch))
        api = c.api
        self.exec_id = api.exec_create(
            container.id,
            ["bash", "-lc", "cd /mnt/user 2>/dev/null || cd /; exec bash"],
            tty=True,
            stdin=True,
            environment={**_env(branch)},
        )["Id"]
        self._socket = api.exec_start(self.exec_id, tty=True, socket=True)
        self.raw = self._socket._sock

    def read(self, size: int = 65536) -> bytes:
        return self.raw.recv(size)

    def write(self, data: bytes) -> None:
        self.raw.sendall(data)

    def resize(self, rows: int, cols: int) -> None:
        try:
            client().api.exec_resize(self.exec_id, height=rows, width=cols)
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        try:
            self.raw.close()
        except Exception:  # noqa: BLE001
            pass
