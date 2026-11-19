"""Server-rendered pages (Jinja2 + HTMX).

Pages:
    /                      dashboard (projects)
    /projects/{id}         branch board (production / staging / development)
    /branches/{id}         branch detail with tabs: history, logs, shell,
                           backups, monitor, settings
    /builds/{id}           single build view with live status + log
Partials (htmx):
    /partials/build/{id}       build status + log tail
    /partials/monitor/{id}     host + container metrics
"""
from __future__ import annotations

import re
import secrets
import shutil
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app import odoo_config, postgres, twofa
from app.auth import (
    can_open_shell,
    clear_pending_2fa,
    current_user,
    has_role,
    login_user,
    logout_user,
    pending_2fa_user,
    set_pending_2fa,
    verify_password,
)
from app.config import settings
from app.db import get_session
from app.errors import DeployError, PlatformError
from app.models import (
    BUILD_QUEUED,
    BUILD_RUNNING,
    RESTORE_QUEUED,
    RESTORE_RUNNING,
    STAGES,
    Backup,
    Branch,
    Build,
    Event,
    Project,
    RestoreJob,
    User,
)
from app.ports import allocate_gevent_port, allocate_port
from app.services import backups as backups_service
from app.services import control_plane, docker_ctl, gitops, monitor, nginx

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# --- helpers -------------------------------------------------------------------


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:40]


def _db_name(project: Project, branch_name: str) -> str:
    return f"paas_{project.slug}_{_slugify(branch_name).replace('-', '_')}"[:60]


def _redact_url(url: str) -> str:
    """Strip credentials from a repository URL before displaying it."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return url
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        return url


def _msg(request: Request) -> str:
    return request.query_params.get("msg", "")


def _ctx(request: Request, user: Optional[User], **extra) -> dict:
    base = {
        "request": request,
        "user": user,
        "app_name": settings.APP_NAME,
        "public_host": settings.PUBLIC_HOST or request.url.hostname,
        "msg": _msg(request),
    }
    base.update(extra)
    return base


def _page_user(request: Request, session: Session):
    user = current_user(request, session)
    if user is None:
        return None, RedirectResponse("/login", status_code=303)
    return user, None


def _back(url: str, message: str = "") -> RedirectResponse:
    if message:
        url = f"{url}{'&' if '?' in url else '?'}msg={quote(message)}"
    return RedirectResponse(url, status_code=303)


def _latest_build(session: Session, branch_id: int) -> Optional[Build]:
    return session.exec(
        select(Build).where(Build.branch_id == branch_id).order_by(Build.id.desc())
    ).first()


def _tail_lines(path: Path, lines: int = 200) -> str:
    if not path.exists():
        return "(no log yet)"
    try:
        content = path.read_text(errors="replace").splitlines()
    except OSError:
        return "(log unreadable)"
    return "\n".join(content[-lines:])


# --- auth routes ---------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", _ctx(request, None))


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
):
    user = session.exec(select(User).where(User.email == email.lower().strip())).first()
    if user is None or not verify_password(password, user.password_hash):
        return _back("/login", "Invalid email or password")
    if user.totp_enabled:
        if not user.totp_secret:
            return _back("/login", "Two-factor authentication is misconfigured for this user")
        response = RedirectResponse("/login/2fa", status_code=303)
        set_pending_2fa(response, user)
        return response
    response = RedirectResponse("/", status_code=303)
    login_user(response, user)
    return response


@router.get("/login/2fa", response_class=HTMLResponse)
def login_2fa_page(request: Request, session: Session = Depends(get_session)):
    user = pending_2fa_user(request, session)
    if user is None:
        response = _back("/login", "Sign in again to continue")
        clear_pending_2fa(response)
        return response
    return templates.TemplateResponse(
        request, "login_2fa.html", _ctx(request, None, email=user.email)
    )


@router.post("/login/2fa")
def login_2fa_submit(
    request: Request,
    code: str = Form(""),
    session: Session = Depends(get_session),
):
    user = pending_2fa_user(request, session)
    if user is None:
        response = _back("/login", "Sign in again to continue")
        clear_pending_2fa(response)
        return response
    if not user.totp_enabled:
        response = RedirectResponse("/", status_code=303)
        login_user(response, user)
        return response
    if twofa.verify_code(user.totp_secret, code):
        response = RedirectResponse("/", status_code=303)
        login_user(response, user)
        return response
    matched, remaining = twofa.consume_recovery_code(
        user.totp_recovery_codes, code
    )
    if matched:
        user.totp_recovery_codes = remaining
        session.add(user)
        session.commit()
        response = RedirectResponse("/", status_code=303)
        login_user(response, user)
        return response
    return _back("/login/2fa", "Invalid two-factor or recovery code")


@router.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    logout_user(response)
    return response


# --- dashboard -------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    projects = session.exec(select(Project).order_by(Project.id)).all()
    rows = []
    for project in projects:
        branches = session.exec(
            select(Branch).where(Branch.project_id == project.id)
        ).all()
        rows.append(
            {
                "project": project,
                "branch_count": len(branches),
                "running": sum(1 for b in branches if b.status == "running"),
                "repo_display": _redact_url(project.repo_url),
                "enterprise": bool((project.enterprise_repo_url or "").strip()),
                "postgres_version": postgres.version_for(project),
            }
        )
    active_odoo_versions = sorted(
        {
            project.odoo_version or settings.ODOO_VERSION
            for project in projects
        },
        reverse=True,
    )
    totals = {
        "projects": len(rows),
        "branches": sum(row["branch_count"] for row in rows),
        "running": sum(row["running"] for row in rows),
        "odoo_versions": len(active_odoo_versions),
    }
    events = session.exec(
        select(Event).order_by(Event.id.desc()).limit(12)
    ).all()
    projects_by_id = {project.id: project for project in projects}
    activity = [
        {"event": event, "project": projects_by_id.get(event.project_id)}
        for event in events
    ]
    odoo_versions = docker_ctl.list_base_versions()
    available_postgres = docker_ctl.list_postgres_versions()
    default_postgres_version = postgres.default_version(
        settings.ODOO_VERSION, available_postgres
    )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _ctx(
            request,
            user,
            rows=rows,
            totals=totals,
            activity=activity,
            odoo_versions=odoo_versions,
            active_odoo_versions=active_odoo_versions,
            postgres_versions=postgres.options(odoo_versions, available_postgres),
            default_odoo_version=settings.ODOO_VERSION,
            default_postgres_version=default_postgres_version,
        ),
    )


def _platform_settings_response(
    request: Request,
    user: User,
    session: Session,
    *,
    recovery_codes: Optional[list] = None,
    message: str = "",
):
    is_admin = has_role(user, "admin")
    setup = bool(user.totp_secret and not user.totp_enabled)
    totp_uri = (
        twofa.provisioning_uri(user.totp_secret, user.email) if setup else ""
    )
    return templates.TemplateResponse(
        request,
        "platform_settings.html",
        _ctx(
            request,
            user,
            is_admin=is_admin,
            status=control_plane.status(session, request) if is_admin else None,
            port=settings.PORT,
            totp_setup=setup,
            totp_uri=totp_uri,
            totp_qr=twofa.qr_svg(totp_uri) if setup else "",
            recovery_codes=recovery_codes or [],
            msg=message or _msg(request),
        ),
    )


@router.get("/settings", response_class=HTMLResponse)
def platform_settings_page(
    request: Request, session: Session = Depends(get_session)
):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    return _platform_settings_response(request, user, session)


@router.post("/settings")
def platform_settings_save(
    request: Request,
    action: str = Form("save"),
    domain: str = Form(""),
    confirm: str = Form(""),
    session: Session = Depends(get_session),
):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    if not has_role(user, "admin"):
        return _back("/", "Only admins can change platform settings")
    try:
        if action == "remove":
            if confirm.strip() != "remove":
                return _back(
                    "/settings", "Type 'remove' to confirm disabling the domain"
                )
            result = control_plane.remove_domain(
                session, request.url.hostname or ""
            )
        else:
            result = control_plane.apply_domain(
                session, domain, settings.ACME_EMAIL
            )
        session.commit()
    except PlatformError as exc:
        session.rollback()
        return _back("/settings", str(exc))
    if result.get("restart"):
        return templates.TemplateResponse(
            request,
            "platform_settings_applying.html",
            _ctx(
                request,
                user,
                target_url=result["target"],
                message=result["message"],
            ),
        )
    return _back("/settings", result["message"])


@router.post("/settings/2fa/setup")
def settings_2fa_setup(
    request: Request, session: Session = Depends(get_session)
):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    if user.totp_enabled:
        return _back("/settings", "Two-factor authentication is already enabled")
    user.totp_secret = twofa.new_secret()
    user.totp_enabled = False
    user.totp_recovery_codes = ""
    session.add(user)
    session.commit()
    return _back("/settings", "Scan the QR code, then verify the 6-digit code")


@router.post("/settings/2fa/verify")
def settings_2fa_verify(
    request: Request,
    code: str = Form(""),
    session: Session = Depends(get_session),
):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    if user.totp_enabled:
        return _back("/settings", "Two-factor authentication is already enabled")
    if not user.totp_secret:
        return _back("/settings", "Start the two-factor setup first")
    if not twofa.verify_code(user.totp_secret, code):
        return _back("/settings", "Invalid authenticator code")
    codes = twofa.generate_recovery_codes()
    user.totp_enabled = True
    user.totp_recovery_codes = twofa.hash_recovery_codes(codes)
    session.add(user)
    session.commit()
    return _platform_settings_response(
        request,
        user,
        session,
        recovery_codes=codes,
        message=(
            "Two-factor authentication enabled. Save these recovery codes now; "
            "they will not be shown again."
        ),
    )


@router.post("/settings/2fa/disable")
def settings_2fa_disable(
    request: Request,
    password: str = Form(""),
    session: Session = Depends(get_session),
):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    if not verify_password(password, user.password_hash):
        return _back("/settings", "Password is incorrect")
    user.totp_enabled = False
    user.totp_secret = ""
    user.totp_recovery_codes = ""
    session.add(user)
    session.commit()
    return _back("/settings", "Two-factor authentication disabled")


@router.post("/settings/2fa/cancel")
def settings_2fa_cancel(
    request: Request, session: Session = Depends(get_session)
):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    if not user.totp_enabled:
        user.totp_secret = ""
        user.totp_recovery_codes = ""
        session.add(user)
        session.commit()
    return _back("/settings", "Two-factor setup cancelled")


@router.post("/projects")
def create_project(
    request: Request,
    slug: str = Form(...),
    display_name: str = Form(""),
    repo_url: str = Form(""),
    enterprise_repo_url: str = Form(""),
    odoo_version: str = Form(""),
    postgres_version: str = Form(""),
    session: Session = Depends(get_session),
):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    slug = _slugify(slug)
    if not slug:
        return _back("/", "Invalid project name")
    if session.exec(select(Project).where(Project.slug == slug)).first():
        return _back("/", f"Project '{slug}' already exists")
    versions = docker_ctl.list_base_versions()
    odoo_version = (odoo_version or settings.ODOO_VERSION).strip()
    if odoo_version not in versions:
        return _back(
            "/",
            f"Odoo {odoo_version} is not available on this server "
            f"(built: {', '.join(versions)}). Build it with "
            f"install.sh --odoo-versions '{odoo_version}'",
        )
    available_postgres = docker_ctl.list_postgres_versions()
    pg_version = (postgres_version or "").strip()
    if not pg_version:
        pg_version = postgres.default_version(odoo_version, available_postgres)
    if pg_version not in postgres.VERSIONS:
        return _back("/", f"Unknown PostgreSQL version '{pg_version}'")
    if pg_version not in available_postgres:
        return _back(
            "/",
            f"PostgreSQL {pg_version} is not provisioned on this server "
            f"(available: {', '.join(available_postgres) or 'none'}). "
            "Provision it with install.sh --postgres-versions "
            f"'{pg_version}'",
        )
    if not postgres.is_compatible(pg_version, odoo_version):
        compatible = ", ".join(postgres.compatible_versions(odoo_version)) or "none"
        return _back(
            "/",
            f"PostgreSQL {pg_version} is not compatible with Odoo {odoo_version} "
            f"(compatible: {compatible})",
        )
    project = Project(
        slug=slug,
        display_name=display_name or slug,
        repo_url=repo_url.strip(),
        enterprise_repo_url=enterprise_repo_url.strip(),
        odoo_version=odoo_version,
        postgres_version=pg_version,
        webhook_secret=secrets.token_hex(24),
    )
    session.add(project)
    session.commit()
    return _back(f"/projects/{project.id}", "Project created")


@router.get("/projects/{project_id}/settings", response_class=HTMLResponse)
def project_settings_page(
    project_id: int, request: Request, session: Session = Depends(get_session)
):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    project = session.get(Project, project_id)
    if project is None:
        return _back("/", "Project not found")
    return templates.TemplateResponse(
        request,
        "project_settings.html",
        _ctx(
            request,
            user,
            project=project,
            postgres_version=postgres.version_for(project),
            config=odoo_config.parse_config(project.odoo_config),
            config_groups=odoo_config.CONFIG_GROUPS,
        ),
    )


@router.post("/projects/{project_id}/settings")
async def project_settings_save(
    project_id: int, request: Request, session: Session = Depends(get_session)
):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    project = session.get(Project, project_id)
    if project is None:
        return _back("/", "Project not found")
    form = await request.form()
    config, error = odoo_config.config_from_form(form)
    if error:
        return _back(f"/projects/{project_id}/settings", f"Invalid settings: {error}")
    old_repo_url = project.repo_url
    old_enterprise_repo_url = project.enterprise_repo_url
    old_config = project.odoo_config
    project.display_name = str(form.get("display_name", "")).strip() or project.slug
    project.repo_url = str(form.get("repo_url", "")).strip()
    project.enterprise_repo_url = str(form.get("enterprise_repo_url", "")).strip()
    project.odoo_config = odoo_config.dump_config(config)
    if (
        project.repo_url != old_repo_url
        or project.enterprise_repo_url != old_enterprise_repo_url
    ):
        message = "Project repository settings updated"
    elif project.odoo_config != old_config:
        message = "Odoo configuration updated"
    else:
        message = "Project settings updated"
    session.add(project)
    session.add(Event(project_id=project.id, kind="info", message=message))
    session.commit()
    return _back(f"/projects/{project_id}/settings", "Settings saved")


@router.post("/projects/{project_id}/delete")
def delete_project(
    project_id: int,
    request: Request,
    confirm: str = Form(""),
    session: Session = Depends(get_session),
):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    project = session.get(Project, project_id)
    if project is None:
        return _back("/", "Project not found")
    if not has_role(user, "admin"):
        return _back(f"/projects/{project_id}", "Only admins can delete projects")
    if confirm.strip() != project.slug:
        return _back(f"/projects/{project_id}", "Type the project name to confirm deletion")
    branches = session.exec(select(Branch).where(Branch.project_id == project_id)).all()
    if any(backups_service.active_restore(session, branch.id) for branch in branches):
        return _back(
            f"/projects/{project_id}",
            "A database restore is queued/running for a branch in this project",
        )
    for branch in branches:
        try:
            nginx.remove(branch)
        except Exception:  # noqa: BLE001 - deletion should not be blocked
            pass
        docker_ctl.remove_container(branch.container_name)
        for job in session.exec(
            select(RestoreJob).where(RestoreJob.branch_id == branch.id)
        ).all():
            session.delete(job)
        session.delete(branch)
    session.delete(project)
    session.commit()
    return _back("/", f"Project '{project.slug}' deleted (databases kept on the server)")


# --- project board ----------------------------------------------------------------


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_page(project_id: int, request: Request, session: Session = Depends(get_session)):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    project = session.get(Project, project_id)
    if project is None:
        return _back("/", "Project not found")
    branches = session.exec(
        select(Branch).where(Branch.project_id == project_id).order_by(Branch.id)
    ).all()
    grouped = {stage: [] for stage in STAGES}
    for branch in branches:
        grouped[branch.stage].append(
            {"branch": branch, "latest": _latest_build(session, branch.id)}
        )
    events = session.exec(
        select(Event)
        .where(Event.project_id == project_id)
        .order_by(Event.id.desc())
        .limit(15)
    ).all()
    webhook_url = (
        f"{control_plane.public_url(session, request)}"
        f"/webhooks/github/{project.id}"
    )
    return templates.TemplateResponse(
        request,
        "project.html",
        _ctx(
            request,
            user,
            project=project,
            postgres_version=postgres.version_for(project),
            repo_display=_redact_url(project.repo_url),
            enterprise=bool((project.enterprise_repo_url or "").strip()),
            grouped=grouped,
            stages=STAGES,
            events=events,
            webhook_url=webhook_url,
        ),
    )


@router.post("/projects/{project_id}/branches")
def create_branch(
    project_id: int,
    request: Request,
    name: str = Form(...),
    stage: str = Form("production"),
    session: Session = Depends(get_session),
):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    project = session.get(Project, project_id)
    if project is None:
        return _back("/", "Project not found")
    name = name.strip()
    if stage not in STAGES:
        return _back(f"/projects/{project_id}", "Invalid stage")
    if session.exec(
        select(Branch).where(Branch.project_id == project_id, Branch.name == name)
    ).first():
        return _back(f"/projects/{project_id}", f"Branch '{name}' already exists")
    if stage == "production" and session.exec(
        select(Branch).where(Branch.project_id == project_id, Branch.stage == "production")
    ).first():
        return _back(f"/projects/{project_id}", "This project already has a production branch")
    try:
        port = allocate_port(session)
    except DeployError as exc:
        return _back(f"/projects/{project_id}", str(exc))
    branch = Branch(
        project_id=project_id,
        name=name,
        stage=stage,
        odoo_version=project.odoo_version or settings.ODOO_VERSION,
        postgres_version=postgres.version_for(project),
        port=port,
        db_name=_db_name(project, name),
        status="new",
    )
    session.add(branch)
    session.commit()
    docker_ctl.ensure_branch_dirs(branch)
    session.add(
        Build(branch_id=branch.id, status=BUILD_QUEUED, step="queued", commit_message="initial build")
    )
    session.add(
        Event(
            project_id=project_id,
            branch_id=branch.id,
            kind="info",
            message=(
                f"Branch '{name}' created at stage {stage} "
                f"(port {port}, PostgreSQL {branch.postgres_version})"
            ),
        )
    )
    session.commit()
    return _back(f"/projects/{project_id}", f"Branch '{name}' created - build queued")


# --- branch routes ------------------------------------------------------------------


@router.get("/branches/{branch_id}", response_class=HTMLResponse)
def branch_page(
    branch_id: int,
    request: Request,
    tab: str = "history",
    session: Session = Depends(get_session),
):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    branch = session.get(Branch, branch_id)
    if branch is None:
        return _back("/", "Branch not found")
    project = session.get(Project, branch.project_id)
    latest = _latest_build(session, branch_id)
    builds = session.exec(
        select(Build).where(Build.branch_id == branch_id).order_by(Build.id.desc()).limit(10)
    ).all()
    events = session.exec(
        select(Event).where(Event.branch_id == branch_id).order_by(Event.id.desc()).limit(25)
    ).all()
    backup_rows = session.exec(
        select(Backup).where(Backup.branch_id == branch_id).order_by(Backup.id.desc()).limit(50)
    ).all()
    restore_job = (
        backups_service.latest_restore(session, branch_id)
        if tab == "backups"
        else None
    )
    restore_active = bool(
        restore_job
        and restore_job.status in (RESTORE_QUEUED, RESTORE_RUNNING)
    )
    restore_log_tail = (
        _tail_lines(Path(restore_job.log_path), 80)
        if restore_job and restore_job.log_path
        else "(waiting for worker...)"
    )
    repo = docker_ctl.repo_dir(branch)
    commits = gitops.recent_commits(repo) if (repo / ".git").exists() else []
    stats = monitor.branch_stats(branch) if tab == "monitor" else None
    project_config = (
        odoo_config.parse_config(project.odoo_config) if project else {}
    )
    proxy_mode = bool(project_config.get("proxy_mode"))
    workers = int(project_config.get("workers") or 0)
    domain_url = ""
    if branch.domain:
        scheme = "https" if nginx.certificate_exists(branch.domain) else "http"
        domain_url = f"{scheme}://{branch.domain}"
    return templates.TemplateResponse(
        request,
        "branch.html",
        _ctx(
            request,
            user,
            project=project,
            branch=branch,
            latest=latest,
            builds=builds,
            events=events,
            backups=backup_rows,
            restore_job=restore_job,
            restore_active=restore_active,
            restore_log_tail=restore_log_tail,
            commits=commits,
            tab=tab,
            stats=stats,
            proxy_mode=proxy_mode,
            workers=workers,
            domain_url=domain_url,
            can_shell=can_open_shell(user, branch.stage),
            container_state=docker_ctl.container_state(branch.container_name),
        ),
    )


@router.get("/branches/{branch_id}/restore-status", response_class=HTMLResponse)
def restore_status_partial(
    branch_id: int, request: Request, session: Session = Depends(get_session)
):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    branch = session.get(Branch, branch_id)
    if branch is None:
        return HTMLResponse("")
    job = backups_service.latest_restore(session, branch_id)
    active = bool(job and job.status in (RESTORE_QUEUED, RESTORE_RUNNING))
    log_tail = (
        _tail_lines(Path(job.log_path), 80)
        if job and job.log_path
        else "(waiting for worker...)"
    )
    return templates.TemplateResponse(
        request,
        "partials/restore_status.html",
        {
            "request": request,
            "branch": branch,
            "restore_job": job,
            "restore_active": active,
            "restore_log_tail": log_tail,
        },
    )


@router.post("/branches/{branch_id}/stage")
def move_stage(
    branch_id: int,
    request: Request,
    stage: str = Form(...),
    session: Session = Depends(get_session),
):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    branch = session.get(Branch, branch_id)
    if branch is None or stage not in STAGES:
        return _back("/", "Invalid request")
    if stage == "production":
        existing = session.exec(
            select(Branch).where(
                Branch.project_id == branch.project_id,
                Branch.stage == "production",
                Branch.id != branch.id,
            )
        ).first()
        if existing:
            return _back(f"/projects/{branch.project_id}", "A production branch already exists")
    branch.stage = stage
    session.add(branch)
    session.add(
        Event(project_id=branch.project_id, branch_id=branch_id, kind="stage", message=f"'{branch.name}' moved to {stage}")
    )
    session.commit()
    return _back(f"/branches/{branch_id}", f"Moved to {stage}")


@router.post("/branches/{branch_id}/deploy")
def deploy_branch(
    branch_id: int,
    request: Request,
    rebuild_base: str = Form(""),
    session: Session = Depends(get_session),
):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    branch = session.get(Branch, branch_id)
    if branch is None:
        return _back("/", "Branch not found")
    if backups_service.active_restore(session, branch_id):
        return _back(
            f"/branches/{branch_id}?tab=backups",
            "A database restore is queued/running; wait for it to finish",
        )
    rebuild = rebuild_base == "1"
    build = Build(
        branch_id=branch_id,
        status=BUILD_QUEUED,
        step="queued",
        rebuild_base=rebuild,
        commit_message="manual deploy + Odoo update" if rebuild else "manual deploy",
    )
    session.add(build)
    branch.status = "building"
    session.add(branch)
    session.commit()
    return _back(f"/builds/{build.id}", "Deploy queued")


@router.post("/branches/{branch_id}/stop")
def stop_branch(branch_id: int, request: Request, session: Session = Depends(get_session)):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    branch = session.get(Branch, branch_id)
    if branch is None:
        return _back("/", "Branch not found")
    if backups_service.active_restore(session, branch_id):
        return _back(
            f"/branches/{branch_id}?tab=backups",
            "A database restore is queued/running; wait for it to finish",
        )
    docker_ctl.stop_container(branch.container_name)
    branch.status = "stopped"
    session.add(branch)
    session.add(Event(project_id=branch.project_id, branch_id=branch_id, kind="info", message="Container stopped"))
    session.commit()
    return _back(f"/branches/{branch_id}", "Container stopped")


@router.post("/branches/{branch_id}/start")
def start_branch(branch_id: int, request: Request, session: Session = Depends(get_session)):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    branch = session.get(Branch, branch_id)
    if branch is None:
        return _back("/", "Branch not found")
    if backups_service.active_restore(session, branch_id):
        return _back(
            f"/branches/{branch_id}?tab=backups",
            "A database restore is queued/running; wait for it to finish",
        )
    project = session.get(Project, branch.project_id)
    enterprise = bool(project and (project.enterprise_repo_url or "").strip())
    try:
        docker_ctl.start_server_container(
            branch, project.odoo_config if project else "", enterprise=enterprise
        )
    except Exception as exc:  # noqa: BLE001 - surfaced in the UI
        return _back(f"/branches/{branch_id}", f"Could not start: {exc}")
    branch.status = "running"
    session.add(branch)
    session.add(Event(project_id=branch.project_id, branch_id=branch_id, kind="info", message="Container started"))
    session.commit()
    return _back(f"/branches/{branch_id}", "Container started")


@router.post("/branches/{branch_id}/settings")
def branch_settings(
    branch_id: int,
    request: Request,
    install_modules: str = Form(""),
    update_on_deploy: str = Form(""),
    tests_enabled: str = Form(""),
    session: Session = Depends(get_session),
):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    branch = session.get(Branch, branch_id)
    if branch is None:
        return _back("/", "Branch not found")
    branch.install_modules = install_modules.strip()
    branch.update_on_deploy = update_on_deploy == "on"
    branch.tests_enabled = tests_enabled == "on"
    session.add(branch)
    session.commit()
    return _back(f"/branches/{branch_id}?tab=settings", "Settings saved")


@router.post("/branches/{branch_id}/domain")
def branch_domain(
    branch_id: int,
    request: Request,
    domain: str = Form(""),
    session: Session = Depends(get_session),
):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    branch = session.get(Branch, branch_id)
    if branch is None:
        return _back("/", "Branch not found")
    project = session.get(Project, branch.project_id)
    project_config = (
        odoo_config.parse_config(project.odoo_config) if project else {}
    )
    proxy_mode = bool(project_config.get("proxy_mode"))
    workers = int(project_config.get("workers") or 0)
    domain = (domain or "").strip().lower().rstrip(".")
    if domain:
        if not proxy_mode:
            return _back(
                f"/branches/{branch_id}?tab=settings",
                "Enable Proxy mode in Project settings before assigning a domain",
            )
        try:
            domain = nginx.normalize_domain(domain)
        except PlatformError as exc:
            return _back(f"/branches/{branch_id}?tab=settings", str(exc))
        existing = session.exec(
            select(Branch).where(Branch.domain == domain, Branch.id != branch_id)
        ).first()
        if existing:
            return _back(
                f"/branches/{branch_id}?tab=settings",
                f"Domain is already assigned to branch '{existing.name}'",
            )
        if not branch.gevent_port:
            try:
                branch.gevent_port = allocate_gevent_port(session)
            except DeployError as exc:
                return _back(f"/branches/{branch_id}?tab=settings", str(exc))
        try:
            result = nginx.provision(
                branch, domain, settings.ACME_EMAIL, workers=workers
            )
        except PlatformError as exc:
            return _back(
                f"/branches/{branch_id}?tab=settings",
                f"Domain not saved: {exc}",
            )
        branch.domain = domain
        session.add(branch)
        session.add(
            Event(
                project_id=branch.project_id,
                branch_id=branch_id,
                kind="info",
                message=f"Domain set to {domain}",
            )
        )
        session.commit()
        return _back(
            f"/branches/{branch_id}?tab=settings",
            f"{result}. Deploy the branch to publish the gevent port.",
        )
    if branch.domain:
        try:
            nginx.remove(branch)
        except PlatformError as exc:
            return _back(
                f"/branches/{branch_id}?tab=settings",
                f"Could not remove nginx config: {exc}",
            )
        old_domain = branch.domain
        branch.domain = ""
        branch.gevent_port = 0
        session.add(branch)
        session.add(
            Event(
                project_id=branch.project_id,
                branch_id=branch_id,
                kind="info",
                message=f"Domain {old_domain} removed",
            )
        )
        session.commit()
        return _back(f"/branches/{branch_id}?tab=settings", "Domain removed")
    return _back(f"/branches/{branch_id}?tab=settings", "No domain configured")


@router.post("/branches/{branch_id}/delete")
def delete_branch(
    branch_id: int,
    request: Request,
    confirm: str = Form(""),
    session: Session = Depends(get_session),
):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    branch = session.get(Branch, branch_id)
    if branch is None:
        return _back("/", "Branch not found")
    if not has_role(user, "admin"):
        return _back(f"/branches/{branch_id}", "Only admins can delete branches")
    if backups_service.active_restore(session, branch_id):
        return _back(
            f"/branches/{branch_id}?tab=backups",
            "A database restore is queued/running; wait for it to finish",
        )
    if confirm.strip() != branch.name:
        return _back(f"/branches/{branch_id}?tab=settings", "Type the branch name to confirm deletion")
    project_id = branch.project_id
    try:
        nginx.remove(branch)
    except Exception:  # noqa: BLE001 - deletion should not be blocked
        pass
    docker_ctl.remove_container(branch.container_name)
    try:
        docker_ctl.drop_database(
            branch.db_name, container=docker_ctl.postgres_container(branch)
        )
    except Exception:  # noqa: BLE001
        pass
    for backup in session.exec(select(Backup).where(Backup.branch_id == branch_id)).all():
        session.delete(backup)
    for build in session.exec(select(Build).where(Build.branch_id == branch_id)).all():
        session.delete(build)
    for job in session.exec(
        select(RestoreJob).where(RestoreJob.branch_id == branch_id)
    ).all():
        session.delete(job)
    session.delete(branch)
    session.add(Event(project_id=project_id, kind="info", message=f"Branch '{branch.name}' deleted"))
    session.commit()
    shutil.rmtree(docker_ctl.branch_dir(branch), ignore_errors=True)
    return _back(f"/projects/{project_id}", f"Branch '{branch.name}' deleted")


# --- build routes ---------------------------------------------------------------------


@router.get("/builds/{build_id}", response_class=HTMLResponse)
def build_page(build_id: int, request: Request, session: Session = Depends(get_session)):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    build = session.get(Build, build_id)
    if build is None:
        return _back("/", "Build not found")
    branch = session.get(Branch, build.branch_id)
    project = session.get(Project, branch.project_id)
    log_tail = _tail_lines(Path(build.log_path) if build.log_path else Path("/nonexistent"))
    return templates.TemplateResponse(
        request,
        "build.html",
        _ctx(request, user, project=project, branch=branch, build=build, log_tail=log_tail),
    )


@router.get("/partials/build/{build_id}", response_class=HTMLResponse)
def build_partial(build_id: int, request: Request, session: Session = Depends(get_session)):
    user = current_user(request, session)
    if user is None:
        return HTMLResponse("")
    build = session.get(Build, build_id)
    if build is None:
        return HTMLResponse("<em>build not found</em>")
    branch = session.get(Branch, build.branch_id)
    log_tail = _tail_lines(Path(build.log_path) if build.log_path else Path("/nonexistent"))
    return templates.TemplateResponse(
        request,
        "partials/build_status.html",
        {"request": request, "build": build, "branch": branch, "log_tail": log_tail},
    )


# --- backups -------------------------------------------------------------------------


@router.post("/branches/{branch_id}/backups/create")
def create_backup(branch_id: int, request: Request, session: Session = Depends(get_session)):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    branch = session.get(Branch, branch_id)
    if branch is None:
        return _back("/", "Branch not found")
    try:
        backup = backups_service.create_backup(branch, kind="manual")
        return _back(f"/branches/{branch_id}?tab=backups", f"Backup #{backup.id} created")
    except Exception as exc:  # noqa: BLE001
        return _back(f"/branches/{branch_id}?tab=backups", f"Backup failed: {exc}")


@router.get("/backups/{backup_id}/download")
def download_backup(backup_id: int, request: Request, session: Session = Depends(get_session)):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    backup = session.get(Backup, backup_id)
    if backup is None or not Path(backup.db_path).exists():
        return _back("/", "Backup file not found")
    return FileResponse(backup.db_path, filename=Path(backup.db_path).name)


@router.post("/backups/{backup_id}/restore")
def restore_backup_route(
    backup_id: int,
    request: Request,
    confirm: str = Form(""),
    session: Session = Depends(get_session),
):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    backup = session.get(Backup, backup_id)
    if backup is None:
        return _back("/", "Backup not found")
    if not has_role(user, "admin"):
        return _back(f"/branches/{backup.branch_id}?tab=backups", "Only admins can restore backups")
    branch = session.get(Branch, backup.branch_id)
    if confirm != "restore":
        return _back(f"/branches/{branch.id}?tab=backups", "Restore cancelled")
    if backups_service.active_restore(session, branch.id):
        return _back(
            f"/branches/{branch.id}?tab=backups",
            "A database restore is already queued/running",
        )
    if session.exec(
        select(Build).where(
            Build.branch_id == branch.id,
            Build.status.in_([BUILD_QUEUED, BUILD_RUNNING]),
        )
    ).first():
        return _back(
            f"/branches/{branch.id}?tab=backups",
            "A build is queued/running; wait for it to finish before restoring",
        )
    job = RestoreJob(
        branch_id=branch.id,
        backup_id=backup.id,
        kind="backup",
        source_name=Path(backup.db_path).name,
        status=RESTORE_QUEUED,
        step="queued",
    )
    session.add(job)
    branch.status = "restoring"
    session.add(branch)
    session.commit()
    return _back(
        f"/branches/{branch.id}?tab=backups",
        f"Restore #{job.id} queued - watch progress below",
    )


@router.post("/branches/{branch_id}/backups/import")
async def import_backup_route(
    branch_id: int,
    request: Request,
    file: UploadFile = File(...),
    confirm: str = Form(""),
    session: Session = Depends(get_session),
):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    branch = session.get(Branch, branch_id)
    if branch is None:
        return _back("/", "Branch not found")
    if not has_role(user, "admin"):
        return _back(
            f"/branches/{branch_id}?tab=backups",
            "Only admins can import databases",
        )
    if confirm != "import":
        return _back(f"/branches/{branch_id}?tab=backups", "Import cancelled")
    if backups_service.active_restore(session, branch_id):
        await file.close()
        return _back(
            f"/branches/{branch_id}?tab=backups",
            "A database restore is already queued/running",
        )
    if session.exec(
        select(Build).where(
            Build.branch_id == branch_id,
            Build.status.in_([BUILD_QUEUED, BUILD_RUNNING]),
        )
    ).first():
        await file.close()
        return _back(
            f"/branches/{branch_id}?tab=backups",
            "A build is queued/running; wait for it to finish before importing",
        )
    dest = backups_service.import_path(branch, file.filename or "backup")
    queued = False
    total = 0
    try:
        with dest.open("wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > backups_service.IMPORT_MAX_BYTES:
                    raise PlatformError("Backup is too large (maximum 2 GB)")
                output.write(chunk)
        if total == 0:
            raise PlatformError("The uploaded backup is empty")
        job = RestoreJob(
            branch_id=branch.id,
            kind="import",
            source_path=str(dest),
            source_name=Path(file.filename or "backup").name[:120],
            status=RESTORE_QUEUED,
            step="queued",
        )
        session.add(job)
        branch.status = "restoring"
        session.add(branch)
        session.commit()
        queued = True
        return _back(
            f"/branches/{branch_id}?tab=backups",
            f"Import #{job.id} queued - watch progress below",
        )
    except Exception as exc:  # noqa: BLE001
        detail = str(exc).replace("\n", " ")[:300]
        return _back(
            f"/branches/{branch_id}?tab=backups", f"Import failed: {detail}"
        )
    finally:
        await file.close()
        if not queued:
            dest.unlink(missing_ok=True)


@router.post("/backups/{backup_id}/delete")
def delete_backup_route(backup_id: int, request: Request, session: Session = Depends(get_session)):
    user, redirect = _page_user(request, session)
    if redirect:
        return redirect
    backup = session.get(Backup, backup_id)
    if backup is None:
        return _back("/", "Backup not found")
    branch_id = backup.branch_id
    if session.exec(
        select(RestoreJob).where(
            RestoreJob.backup_id == backup_id,
            RestoreJob.status.in_([RESTORE_QUEUED, RESTORE_RUNNING]),
        )
    ).first():
        return _back(
            f"/branches/{branch_id}?tab=backups",
            "This backup is queued/running for a restore and cannot be deleted",
        )
    backups_service.delete_backup(backup)
    return _back(f"/branches/{branch_id}?tab=backups", "Backup deleted")


# --- monitor partial -------------------------------------------------------------------


@router.get("/partials/monitor/{branch_id}", response_class=HTMLResponse)
def monitor_partial(branch_id: int, request: Request, session: Session = Depends(get_session)):
    user = current_user(request, session)
    if user is None:
        return HTMLResponse("")
    branch = session.get(Branch, branch_id)
    if branch is None:
        return HTMLResponse("<em>branch not found</em>")
    return templates.TemplateResponse(
        request,
        "partials/monitor.html",
        {"request": request, "branch": branch, "stats": monitor.branch_stats(branch)},
    )
