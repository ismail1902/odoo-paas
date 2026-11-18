"""Build pipeline.

Steps (simplified Odoo.sh semantics):
  1. cloning      - clone/fetch the branch at the requested commit
  2. installing   - per-branch venv + requirements.txt files
  3. initializing - create DB (with modules) or `-u all` update
  4. testing      - development branches: run unit tests during install
  5. starting     - (re)start the Odoo server container + HTTP healthcheck

Everything is written to ``LOG_ROOT/build-<id>.log`` and progress is stored on
the Build row (``status`` / ``step``). Failures set the branch status to failed
and stop the container. The production rollback limitation is documented in
docs/RUNBOOK.md.
"""
from __future__ import annotations

import shutil
import time
import urllib.request
from datetime import datetime, timezone

from app.config import settings
from app.db import session_scope
from app.errors import BuildError
from app.models import (
    BUILD_FAILED,
    BUILD_RUNNING,
    BUILD_SUCCEEDED,
    Branch,
    Build,
    Event,
    Project,
)
from app.services import docker_ctl, gitops


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def run_build(build_id: int) -> None:
    # --- snapshot the job (detached ORM objects; scalar fields are loaded) ---
    with session_scope() as session:
        build = session.get(Build, build_id)
        if build is None:
            return
        branch = session.get(Branch, build.branch_id)
        project = session.get(Project, branch.project_id)
        build.status = BUILD_RUNNING
        build.step = "preparing"
        build.started_at = build.started_at or _now()
        session.add(build)
        session.add(
            Event(
                project_id=project.id,
                branch_id=branch.id,
                build_id=build.id,
                kind="deploy",
                message=f"Build #{build.id} started for '{branch.name}'",
            )
        )
        repo_url = (project.repo_url or "").strip()
        log_path = settings.LOG_ROOT / f"build-{build_id}.log"
        build.log_path = str(log_path)
        session.add(build)

    def log(line: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with log_path.open("a") as handle:
            handle.write(f"{stamp} {line}\n")

    def step(name: str) -> None:
        log(f"==> {name}")
        with session_scope() as session:
            row = session.get(Build, build_id)
            row.step = name
            session.add(row)

    try:
        log(
            f"Build #{build_id} | branch '{branch.name}' | stage {branch.stage} "
            f"| odoo {branch.odoo_version or settings.ODOO_VERSION} "
            f"| port {branch.port} | db {branch.db_name}"
        )
        if not docker_ctl.available():
            raise BuildError("Docker is not available on this host")
        version = branch.odoo_version or settings.ODOO_VERSION
        if build.rebuild_base:
            step("updating Odoo base image")
            log(
                f"pulling latest Odoo {version} source and rebuilding "
                f"{docker_ctl.image_name(version)} (this can take several minutes)"
            )
            docker_ctl.build_base_image(version, log)
            log(f"Odoo {version} base image updated")
        if not docker_ctl.image_exists(version):
            raise BuildError(
                f"Base image for Odoo {version} is not built on this host "
                f"(run: sudo ./install.sh --odoo-versions '{version}')"
            )

        step("cloning repository")
        repository = docker_ctl.repo_dir(branch)
        repo_sha = gitops.sync(repository, repo_url, branch.name, build.commit_sha or None)
        repo_empty = repo_sha is None
        if repo_empty:
            if repo_url:
                log(
                    "branch has no commits yet - deploying base Odoo without custom "
                    "addons (they install automatically on your first push)"
                )
            else:
                log(
                    "no repository configured - deploying base Odoo without custom "
                    "addons (add a repository in Project settings, then Deploy)"
                )
        else:
            info = gitops.commit_info(repository)
            with session_scope() as session:
                row = session.get(Build, build_id)
                row.commit_sha = info["sha"]
                row.commit_message = info["message"]
                row.commit_author = info["author"]
                session.add(row)
            log(f"commit {info['sha'][:10]} by {info['author']}: {info['message']}")

        enterprise_url = (project.enterprise_repo_url or "").strip()
        if enterprise_url:
            step("syncing enterprise addons")
            version = branch.odoo_version or settings.ODOO_VERSION
            enterprise_sha = gitops.sync(
                docker_ctl.enterprise_dir(branch), enterprise_url, version
            )
            if enterprise_sha is None:
                raise BuildError(
                    f"enterprise addons repository has no branch '{version}' "
                    "(it must match the project's Odoo version)"
                )
            log(f"enterprise addons {enterprise_sha[:10]} ({version})")
        else:
            shutil.rmtree(docker_ctl.enterprise_dir(branch), ignore_errors=True)
        has_enterprise = bool(enterprise_url)

        step("installing requirements")
        code = docker_ctl.ensure_venv(branch, log)
        if code != 0:
            raise BuildError(f"pip install failed (exit code {code})")

        step("initializing database")
        created = docker_ctl.ensure_database(branch.db_name)
        if docker_ctl.vector_extension_version(branch.db_name) is None:
            log("warning: pgvector is unavailable - Odoo 19/20 AI features require it")
        configured = [
            m.strip() for m in (branch.install_modules or "").split(",") if m.strip()
        ]
        demo = branch.stage == "development"
        # Only the configured modules are tested - never the whole base/web
        # suite (its PDF/barcode/font tests need tooling the image may lack).
        run_tests = branch.stage == "development" and branch.tests_enabled and bool(configured)
        if created or not docker_ctl.database_initialized(branch.db_name):
            if not created:
                log("database exists but was never initialized - initializing now")
            modules = branch.install_modules or "base"
            if repo_empty and configured:
                modules = "base"
                if repo_url:
                    log(
                        f"repository is empty - installing base only; "
                        f"'{', '.join(configured)}' install after you push"
                    )
                else:
                    log(
                        f"no repository configured - installing base only; "
                        f"'{', '.join(configured)}' install after you add a repository and deploy"
                    )
            test_tags = ",".join(configured) if run_tests and not repo_empty else ""
            command = docker_ctl.task_command(
                branch,
                install=modules,
                demo=demo,
                tests=bool(test_tags),
                test_tags=test_tags,
                enterprise=has_enterprise,
            )
            log(f"odoo init: {' '.join(command)}")
            code = docker_ctl.run_oneoff(branch, command, log)
            if code != 0:
                raise BuildError(f"database initialization failed (exit code {code})")
        else:
            missing = []
            if configured and not repo_empty:
                installed = docker_ctl.installed_modules(branch.db_name, configured)
                missing = [m for m in configured if m not in installed]
            if missing:
                log(f"installing new module(s): {', '.join(missing)}")
                test_tags = ",".join(missing) if run_tests else ""
                command = docker_ctl.task_command(
                    branch,
                    install=",".join(missing),
                    demo=demo,
                    tests=bool(test_tags),
                    test_tags=test_tags,
                    enterprise=has_enterprise,
                )
                log(f"odoo install: {' '.join(command)}")
                code = docker_ctl.run_oneoff(branch, command, log)
                if code != 0:
                    raise BuildError(f"module installation failed (exit code {code})")
                if build.rebuild_base:
                    command = docker_ctl.task_command(
                        branch, update=True, enterprise=has_enterprise
                    )
                    log(f"odoo update after base image change: {' '.join(command)}")
                    code = docker_ctl.run_oneoff(branch, command, log)
                    if code != 0:
                        raise BuildError(f"module update failed (exit code {code})")
            elif branch.update_on_deploy or build.rebuild_base:
                command = docker_ctl.task_command(
                    branch, update=True, enterprise=has_enterprise
                )
                log(f"odoo update: {' '.join(command)}")
                code = docker_ctl.run_oneoff(branch, command, log)
                if code != 0:
                    raise BuildError(f"module update failed (exit code {code})")
            else:
                log(
                    "database exists - skipping module update "
                    "(enable 'Update modules on deploy' in the Settings tab to change this)"
                )

        step("starting server")
        docker_ctl.ensure_network()
        docker_ctl.start_server_container(
            branch, project.odoo_config, enterprise=has_enterprise
        )
        if not settings.MOCK_DOCKER:
            _wait_http(branch.port, log)

        with session_scope() as session:
            row = session.get(Build, build_id)
            row.status = BUILD_SUCCEEDED
            row.step = "running"
            row.finished_at = _now()
            session.add(row)
            br = session.get(Branch, branch.id)
            br.status = "running"
            br.updated_at = _now()
            session.add(br)
            session.add(
                Event(
                    project_id=project_id_of(branch.id),
                    branch_id=branch.id,
                    build_id=build_id,
                    kind="deploy",
                    message=f"Build #{build_id} succeeded - http://{settings.PUBLIC_HOST or 'server'}:{branch.port}",
                )
            )
        log("BUILD SUCCEEDED")

    except Exception as exc:  # noqa: BLE001 - any failure fails the build
        message = str(exc)
        log(f"BUILD FAILED: {message}")
        try:
            docker_ctl.stop_container(docker_ctl.container_name(branch))
        except Exception:  # noqa: BLE001
            pass
        with session_scope() as session:
            row = session.get(Build, build_id)
            row.status = BUILD_FAILED
            row.step = message[:200]
            row.finished_at = _now()
            session.add(row)
            br = session.get(Branch, branch.id)
            br.status = "failed"
            br.updated_at = _now()
            session.add(br)
            session.add(
                Event(
                    project_id=project_id_of(branch.id),
                    branch_id=branch.id,
                    build_id=build_id,
                    kind="error",
                    message=f"Build #{build_id} failed: {message[:180]}",
                )
            )


def project_id_of(branch_id: int) -> int:
    with session_scope() as session:
        branch = session.get(Branch, branch_id)
        return branch.project_id if branch else 0


def _wait_http(port: int, log, timeout: int = 90) -> None:
    url = f"http://127.0.0.1:{port}/web/login"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status < 500:
                    log(f"server answered with HTTP {response.status}")
                    return
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(3)
    raise BuildError(f"server did not answer on port {port} within {timeout}s")
