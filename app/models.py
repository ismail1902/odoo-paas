"""Database models (SQLite via SQLModel).

Entity map:
    User     1--* Project collaborator roles are platform-wide in v1.
    Project  1--* Branch
    Branch   1--* Build
    Branch   1--* Backup
    Project  1--* Event (activity feed; branch_id/build_id optional)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import NaiveDatetime
from sqlmodel import Field, SQLModel

# --- Constants -------------------------------------------------------------

STAGES = ("production", "staging", "development")

ROLES = ("admin", "developer", "tester")

ROLE_RANK = {"developer": 0, "tester": 1, "admin": 2}

BUILD_QUEUED = "queued"
BUILD_RUNNING = "running"
BUILD_SUCCEEDED = "succeeded"
BUILD_FAILED = "failed"
BUILD_STOPPED = "stopped"


def _now() -> datetime:
    """Naive UTC timestamp (SQLModel stores timezone-free values)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- Models ----------------------------------------------------------------


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    name: str = ""
    password_hash: str
    role: str = "admin"
    created_at: NaiveDatetime = Field(default_factory=_now)


class Project(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True)
    display_name: str = ""
    repo_url: str = ""
    enterprise_repo_url: str = ""  # optional Odoo Enterprise addons checkout
    odoo_version: str = ""  # e.g. "17.0" - picks odoo-paas/base:<version>
    odoo_config: str = ""  # JSON object, see app/odoo_config.py
    webhook_secret: str = ""
    created_at: NaiveDatetime = Field(default_factory=_now)


class Branch(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(index=True, foreign_key="project.id")
    name: str  # git branch name
    stage: str = "development"
    odoo_version: str = ""  # copied from the project at creation time
    port: int = 0
    db_name: str = ""
    status: str = "new"  # new|building|running|failed|stopped
    # Branch settings (the Settings tab)
    install_modules: str = ""  # comma-separated; empty => "base"
    tests_enabled: bool = True
    update_on_deploy: bool = False
    created_at: NaiveDatetime = Field(default_factory=_now)
    updated_at: NaiveDatetime = Field(default_factory=_now)

    @property
    def container_name(self) -> str:
        return f"odoo-paas-branch-{self.id}"


class Build(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    branch_id: int = Field(index=True, foreign_key="branch.id")
    status: str = BUILD_QUEUED  # see BUILD_* constants
    step: str = "queued"
    commit_sha: Optional[str] = None
    commit_message: str = ""
    commit_author: str = ""
    log_path: str = ""
    created_at: NaiveDatetime = Field(default_factory=_now)
    started_at: Optional[NaiveDatetime] = None
    finished_at: Optional[NaiveDatetime] = None


class Backup(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    branch_id: int = Field(index=True, foreign_key="branch.id")
    kind: str = "manual"  # manual|auto|update
    db_path: str = ""
    filestore_path: str = ""
    size_bytes: int = 0
    note: str = ""
    created_at: NaiveDatetime = Field(default_factory=_now)


class Event(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    branch_id: Optional[int] = Field(default=None, index=True)
    build_id: Optional[int] = Field(default=None, index=True)
    kind: str = "info"  # info|deploy|stage|backup|error
    message: str = ""
    created_at: NaiveDatetime = Field(default_factory=_now)


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = ""
