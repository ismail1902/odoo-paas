"""Backups: pg_dump + filestore tarball, retention buckets, restore.

Retention (approximation of the Odoo.sh policy, see docs/RUNBOOK.md):
  - nothing younger than 3 days is ever deleted automatically
  - then keep the first backup of each of the last 7 days,
    4 weeks and 3 months; delete the rest.
"""
from __future__ import annotations

import shutil
import tarfile
import time
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


def backup_dir(branch) -> Path:
    return settings.BACKUP_ROOT / f"project-{branch.project_id}" / f"branch-{branch.id}"


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
