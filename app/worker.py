"""Background worker thread.

- Claims queued builds one at a time and runs the pipeline.
- Recovers stale builds on startup (mark anything left `running` as failed).
- Runs the backup retention policy hourly.
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime, timezone

from sqlmodel import select

from app.config import settings
from app.db import session_scope
from app.models import BUILD_FAILED, BUILD_QUEUED, BUILD_RUNNING, Branch, Build
from app.services import backups, builder

_started = False
_start_lock = threading.Lock()
_last_retention = 0.0


def start_worker() -> None:
    global _started
    with _start_lock:
        if _started or not settings.WORKER_ENABLED:
            return
        _started = True
    _recover_stale_builds()
    thread = threading.Thread(target=_loop, name="odoo-paas-worker", daemon=True)
    thread.start()


def _recover_stale_builds() -> None:
    """Builds interrupted by a platform restart are marked failed."""
    with session_scope() as session:
        stale = session.exec(select(Build).where(Build.status == BUILD_RUNNING)).all()
        for build in stale:
            build.status = BUILD_FAILED
            build.step = "interrupted by platform restart"
            build.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
            session.add(build)


def _loop() -> None:
    while True:
        try:
            _tick()
        except Exception:  # noqa: BLE001 - keep the worker alive
            traceback.print_exc()
        time.sleep(2)


def _tick() -> None:
    build_id = None
    with session_scope() as session:
        build = session.exec(
            select(Build).where(Build.status == BUILD_QUEUED).order_by(Build.id)
        ).first()
        if build is not None:
            build.status = BUILD_RUNNING
            build.step = "claimed by worker"
            session.add(build)
            build_id = build.id
    if build_id is not None:
        builder.run_build(build_id)
        return
    _maybe_retention()


def _maybe_retention() -> None:
    global _last_retention
    if time.time() - _last_retention < 3600:
        return
    _last_retention = time.time()
    with session_scope() as session:
        branch_ids = [branch.id for branch in session.exec(select(Branch)).all()]
    for branch_id in branch_ids:
        try:
            backups.run_retention(branch_id)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
