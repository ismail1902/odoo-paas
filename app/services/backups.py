"""Backups: pg_dump + filestore tarball, retention buckets, restore.

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
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Set

from sqlmodel import select

from app.config import settings
from app.db import session_scope
from app.errors import PlatformError
from app.models import Backup, Branch, Event
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


def _import_zip(branch, src: Path) -> None:
    work = settings.BACKUP_ROOT / "imports" / f"extract-{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        dump_path, filestore = _extract_odoo_zip(src, work)
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
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(filestore), str(dest))
            docker_ctl.make_tree_container_writable(dest)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def import_database(branch, src: Path, original_name: str) -> str:
    """Restore an uploaded PostgreSQL/Odoo backup into a branch."""
    fmt = _import_format(src)
    docker_ctl.stop_container(docker_ctl.container_name(branch))
    if fmt == "zip":
        _import_zip(branch, src)
        label = "Odoo zip"
    elif fmt == "pgdump":
        docker_ctl.restore_database(
            branch.db_name,
            src,
            container=docker_ctl.postgres_container(branch),
        )
        label = "PostgreSQL dump"
    else:
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


def restore_backup(branch, backup: Backup) -> None:
    db_path = Path(backup.db_path)
    if not db_path.exists():
        raise PlatformError("backup dump file is missing on disk")
    docker_ctl.stop_container(docker_ctl.container_name(branch))
    docker_ctl.restore_database(
        branch.db_name,
        db_path,
        container=docker_ctl.postgres_container(branch),
    )
    if backup.filestore_path and Path(backup.filestore_path).exists():
        base = docker_ctl.filestore_dir(branch)
        if base.exists():
            shutil.rmtree(base)
        base.mkdir(parents=True, exist_ok=True)
        with tarfile.open(backup.filestore_path, "r:gz") as tar:
            try:
                tar.extractall(base, filter="data")  # py3.12+
            except TypeError:
                tar.extractall(base)
        docker_ctl.make_tree_container_writable(base)
    with session_scope() as session:
        session.add(
            Event(
                project_id=branch.project_id,
                branch_id=branch.id,
                kind="backup",
                message=f"Backup #{backup.id} restored into '{branch.name}' (container stopped)",
            )
        )


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
