"""Backups: pg_dump + filestore tarball, retention buckets, restore/import.

Restores run as durable ``RestoreJob`` rows in the single platform worker so
the UI can follow a log without holding an HTTP request open.

Retention (approximation of the Odoo.sh policy, see docs/RUNBOOK.md):
  - nothing younger than 3 days is ever deleted automatically
  - then keep the first backup of each of the last 7 days,
    4 weeks and 3 months; delete the rest.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import tarfile
import time
import traceback
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Set

from sqlmodel import Session, select

from app.config import settings
from app.db import session_scope
from app.errors import PlatformError
from app.models import (
    RESTORE_FAILED,
    RESTORE_QUEUED,
    RESTORE_RUNNING,
    RESTORE_SUCCEEDED,
    Backup,
    Branch,
    Event,
    RestoreJob,
)
from app.services import docker_ctl

RECENT_KEEP_DAYS = 3
DAILY_KEEP = 7
WEEKLY_KEEP = 4
MONTHLY_KEEP = 3

IMPORT_MAX_BYTES = 2 * 1024**3
IMPORT_MAX_EXTRACTED_BYTES = 6 * 1024**3


def backup_dir(branch) -> Path:
    return settings.BACKUP_ROOT / f"project-{branch.project_id}" / f"branch-{branch.id}"


def import_path(branch, filename: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename or "backup").name)[:80]
    safe = safe or "backup"
    out = settings.BACKUP_ROOT / "imports"
    out.mkdir(parents=True, exist_ok=True)
    return out / f"branch-{branch.id}-{time.time_ns()}-{safe}"


def active_restore(session: Session, branch_id: int) -> Optional[RestoreJob]:
    return session.exec(
        select(RestoreJob)
        .where(
            RestoreJob.branch_id == branch_id,
            RestoreJob.status.in_([RESTORE_QUEUED, RESTORE_RUNNING]),
        )
        .order_by(RestoreJob.id.desc())
    ).first()


def latest_restore(session: Session, branch_id: int) -> Optional[RestoreJob]:
    return session.exec(
        select(RestoreJob)
        .where(RestoreJob.branch_id == branch_id)
        .order_by(RestoreJob.id.desc())
    ).first()


def create_backup(branch, *, kind: str = "manual", note: str = "") -> Backup:
    out = backup_dir(branch)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    db_file = out / f"db-{stamp}.dump"
    fs_file = out / f"filestore-{stamp}.tgz"

    docker_ctl.dump_database(
        branch.db_name,
        db_file,
        container=docker_ctl.postgres_container(branch),
    )
    base = docker_ctl.filestore_dir(branch)
    with tarfile.open(fs_file, "w:gz") as tar:
        if base.exists():
            tar.add(base, arcname=".")
    size = db_file.stat().st_size + fs_file.stat().st_size

    with session_scope() as session:
        backup = Backup(
            branch_id=branch.id,
            kind=kind,
            db_path=str(db_file),
            filestore_path=str(fs_file),
            size_bytes=size,
            note=note,
        )
        session.add(backup)
        session.commit()
        session.refresh(backup)
        session.add(
            Event(
                project_id=branch.project_id,
                branch_id=branch.id,
                kind="backup",
                message=f"{kind} backup created ({size // 1024} KB)",
            )
        )
        return backup


def run_retention(branch_id: int) -> int:
    """Delete backups beyond the retention policy. Returns deletion count."""
    deleted = 0
    with session_scope() as session:
        rows = session.exec(
            select(Backup).where(Backup.branch_id == branch_id).order_by(Backup.created_at.desc())
        ).all()
        keep = _select_keep(rows)
        for backup in rows:
            if backup.id in keep:
                continue
            for path in (backup.db_path, backup.filestore_path):
                if path:
                    try:
                        Path(path).unlink(missing_ok=True)
                    except OSError:
                        pass
            session.delete(backup)
            deleted += 1
    return deleted


def _select_keep(backups: list) -> Set[int]:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(days=RECENT_KEEP_DAYS)
    keep: Set[int] = set()
    days, weeks, months = set(), set(), set()
    for backup in backups:  # newest first
        if backup.created_at >= cutoff:
            keep.add(backup.id)
            continue
        day = backup.created_at.date()
        week = day.isocalendar()[:2]
        month = (day.year, day.month)
        if len(days) < DAILY_KEEP and day not in days:
            days.add(day)
            keep.add(backup.id)
        elif len(weeks) < WEEKLY_KEEP and week not in weeks:
            weeks.add(week)
            keep.add(backup.id)
        elif len(months) < MONTHLY_KEEP and month not in months:
            months.add(month)
            keep.add(backup.id)
    return keep


def _import_format(src: Path) -> str:
    with src.open("rb") as fh:
        magic = fh.read(8)
    if magic[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    if magic[:2] == b"\x1f\x8b":
        return "sql.gz"
    if magic[:5] == b"PGDMP":
        return "pgdump"
    if b"\x00" in magic:
        raise PlatformError(
            "Unsupported backup format. Use a PostgreSQL .dump, .sql, .sql.gz or Odoo .zip file."
        )
    return "sql"


def _extract_odoo_zip(src: Path, dest: Path) -> tuple:
    with zipfile.ZipFile(src) as archive:
        infos = archive.infolist()
        if not any(
            info.filename == "dump.sql" and not info.is_dir() for info in infos
        ):
            raise PlatformError("The zip does not contain a dump.sql file")
        total = sum(info.file_size for info in infos)
        if total > IMPORT_MAX_EXTRACTED_BYTES:
            raise PlatformError("The extracted backup is too large (max 6 GB)")
        root = dest.resolve()
        for info in infos:
            name = info.filename
            if name != "dump.sql" and not name.startswith("filestore/"):
                continue
            if name.startswith(("/", "\\")) or ".." in Path(name).parts:
                raise PlatformError("The zip contains an unsafe path")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise PlatformError("The zip contains a symbolic link")
            target = (dest / name).resolve()
            if target != root and not str(target).startswith(str(root) + os.sep):
                raise PlatformError("The zip contains an unsafe path")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    dump_path = dest / "dump.sql"
    filestore = dest / "filestore"
    return dump_path, (filestore if filestore.exists() else None)


def _log(log: Optional[Callable[[str], None]], message: str) -> None:
    if log is not None:
        log(message)


def _import_zip(
    branch, src: Path, log: Optional[Callable[[str], None]] = None
) -> None:
    work = settings.BACKUP_ROOT / "imports" / f"extract-{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        _log(log, "extracting Odoo zip")
        dump_path, filestore = _extract_odoo_zip(src, work)
        _log(log, "restoring database from dump.sql")
        docker_ctl.restore_sql_database(
            branch.db_name,
            dump_path,
            container=docker_ctl.postgres_container(branch),
        )
        if filestore is not None:
            dest = (
                docker_ctl.filestore_dir(branch)
                / "filestore"
                / branch.db_name
            )
            _log(log, f"restoring filestore to {dest}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(filestore), str(dest))
            docker_ctl.make_tree_container_writable(dest)
        else:
            _log(log, "zip has no filestore; existing filestore left unchanged")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def import_database(
    branch,
    src: Path,
    original_name: str,
    log: Optional[Callable[[str], None]] = None,
) -> str:
    """Restore an uploaded PostgreSQL/Odoo backup into a branch."""
    _log(log, f"detecting backup format for {Path(original_name).name}")
    fmt = _import_format(src)
    _log(log, f"detected {fmt}; stopping branch container")
    docker_ctl.stop_container(docker_ctl.container_name(branch))
    if fmt == "zip":
        _import_zip(branch, src, log=log)
        label = "Odoo zip"
    elif fmt == "pgdump":
        _log(log, "restoring PostgreSQL custom dump")
        docker_ctl.restore_database(
            branch.db_name,
            src,
            container=docker_ctl.postgres_container(branch),
        )
        label = "PostgreSQL dump"
    else:
        _log(log, f"restoring {fmt} dump")
        docker_ctl.restore_sql_database(
            branch.db_name,
            src,
            container=docker_ctl.postgres_container(branch),
            compressed=fmt == "sql.gz",
        )
        label = "SQL dump"
    with session_scope() as session:
        session.add(
            Event(
                project_id=branch.project_id,
                branch_id=branch.id,
                kind="backup",
                message=(
                    f"Imported {label} '{Path(original_name).name[:120]}' "
                    f"into '{branch.name}' (container stopped)"
                ),
            )
        )
    return label


def restore_backup(
    branch,
    backup: Backup,
    log: Optional[Callable[[str], None]] = None,
) -> None:
    db_path = Path(backup.db_path)
    if not db_path.exists():
        raise PlatformError("backup dump file is missing on disk")
    _log(log, f"stopping branch container for backup #{backup.id}")
    docker_ctl.stop_container(docker_ctl.container_name(branch))
    _log(log, "restoring database dump")
    docker_ctl.restore_database(
        branch.db_name,
        db_path,
        container=docker_ctl.postgres_container(branch),
    )
    if backup.filestore_path and Path(backup.filestore_path).exists():
        base = docker_ctl.filestore_dir(branch)
        _log(log, f"restoring filestore from {backup.filestore_path}")
        if base.exists():
            shutil.rmtree(base)
        base.mkdir(parents=True, exist_ok=True)
        with tarfile.open(backup.filestore_path, "r:gz") as tar:
            try:
                tar.extractall(base, filter="data")  # py3.12+
            except TypeError:
                tar.extractall(base)
        docker_ctl.make_tree_container_writable(base)
    else:
        _log(log, "backup has no filestore archive")
    with session_scope() as session:
        session.add(
            Event(
                project_id=branch.project_id,
                branch_id=branch.id,
                kind="backup",
                message=f"Backup #{backup.id} restored into '{branch.name}' (container stopped)",
            )
        )


def run_restore_job(job_id: int) -> None:
    """Execute a queued restore/import job with a persistent log."""
    with session_scope() as session:
        job = session.get(RestoreJob, job_id)
        if job is None or job.status != RESTORE_QUEUED:
            return
        branch = session.get(Branch, job.branch_id)
        if branch is None:
            job.status = RESTORE_FAILED
            job.step = "failed"
            job.error = "branch not found"
            job.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
            session.add(job)
            return
        backup = session.get(Backup, job.backup_id) if job.backup_id else None
        kind = job.kind
        source_path = job.source_path
        source_name = job.source_name

    settings.LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = settings.LOG_ROOT / f"restore-{job_id}.log"

    def log(message: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with log_path.open("a") as handle:
            handle.write(f"{stamp} {message}\n")

    with session_scope() as session:
        job = session.get(RestoreJob, job_id)
        if job is None:
            return
        job.status = RESTORE_RUNNING
        job.step = "starting"
        job.log_path = str(log_path)
        job.started_at = job.started_at or datetime.now(timezone.utc).replace(tzinfo=None)
        branch = session.get(Branch, job.branch_id)
        if branch is not None:
            branch.status = "restoring"
            session.add(branch)
        session.add(job)

    log(f"Restore #{job_id} | branch '{branch.name}' | kind {kind}")
    try:
        if kind == "backup":
            if backup is None:
                raise PlatformError("backup not found")
            log(f"restoring backup #{backup.id}")
            restore_backup(branch, backup, log=log)
        else:
            if not source_path:
                raise PlatformError("uploaded backup file is missing")
            log(f"importing {source_name or Path(source_path).name}")
            import_database(branch, Path(source_path), source_name, log=log)
        with session_scope() as session:
            job = session.get(RestoreJob, job_id)
            if job is not None:
                job.status = RESTORE_SUCCEEDED
                job.step = "done"
                job.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
                session.add(job)
            branch = session.get(Branch, job.branch_id) if job is not None else None
            if branch is not None:
                branch.status = "stopped"
                session.add(branch)
        log("restore complete; container remains stopped")
    except Exception as exc:  # noqa: BLE001 - surfaced in the job log/status
        detail = str(exc).replace("\n", " ")[:2000]
        log(f"ERROR: {detail}")
        log(traceback.format_exc())
        with session_scope() as session:
            job = session.get(RestoreJob, job_id)
            branch = session.get(Branch, job.branch_id) if job is not None else None
            if job is not None:
                job.status = RESTORE_FAILED
                job.step = "failed"
                job.error = detail
                job.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
                session.add(job)
            if branch is not None:
                branch.status = "stopped"
                session.add(branch)
                session.add(
                    Event(
                        project_id=branch.project_id,
                        branch_id=branch.id,
                        kind="error",
                        message=f"Restore #{job_id} failed: {detail[:200]}",
                    )
                )
    finally:
        if kind == "import" and source_path:
            Path(source_path).unlink(missing_ok=True)


def delete_backup(backup: Backup) -> None:
    for path in (backup.db_path, backup.filestore_path):
        if path:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
    with session_scope() as session:
        row = session.get(Backup, backup.id)
        if row:
            session.delete(row)


def find_backup(backup_id: int) -> Optional[Backup]:
    with session_scope() as session:
        return session.get(Backup, backup_id)
