"""Database engine, session helpers and first-run seeding."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlmodel import Session, SQLModel, create_engine, select

from app.config import settings
from app.models import User

engine = create_engine(
    settings.db_url,
    connect_args={"check_same_thread": False},
    echo=False,
)


def _session() -> Session:
    # expire_on_commit=False keeps loaded attributes usable after commit/close
    # (the worker and the request layer hand detached objects to services).
    return Session(engine, expire_on_commit=False)


def init_db() -> None:
    """Create tables, apply light migrations and seed the first admin."""
    SQLModel.metadata.create_all(engine)
    _migrate()
    with _session() as session:
        has_user = session.exec(select(User)).first() is not None
        if not has_user and settings.ADMIN_EMAIL and settings.ADMIN_PASSWORD:
            from app.auth import hash_password

            session.add(
                User(
                    email=settings.ADMIN_EMAIL.lower(),
                    name="Admin",
                    password_hash=hash_password(settings.ADMIN_PASSWORD),
                    role="admin",
                )
            )
            session.commit()


def get_session() -> Iterator[Session]:
    """FastAPI dependency: yields a session per request."""
    with _session() as session:
        yield session


def _migrate() -> None:
    """Additive SQLite migrations (create_all does not alter existing tables)."""
    with engine.begin() as conn:
        for table in ("project", "branch"):
            columns = {
                row[1]
                for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            }
            if "odoo_version" not in columns:
                conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN odoo_version VARCHAR DEFAULT ''"
                )
        conn.exec_driver_sql(
            "UPDATE project SET odoo_version = ? WHERE odoo_version IS NULL OR odoo_version = ''",
            (settings.ODOO_VERSION,),
        )
        conn.exec_driver_sql(
            "UPDATE branch SET odoo_version = ? WHERE odoo_version IS NULL OR odoo_version = ''",
            (settings.ODOO_VERSION,),
        )
        project_columns = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(project)").fetchall()
        }
        if "odoo_config" not in project_columns:
            conn.exec_driver_sql(
                "ALTER TABLE project ADD COLUMN odoo_config VARCHAR DEFAULT ''"
            )
        if "enterprise_repo_url" not in project_columns:
            conn.exec_driver_sql(
                "ALTER TABLE project ADD COLUMN enterprise_repo_url VARCHAR DEFAULT ''"
            )


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager for background tasks (worker, CLI)."""
    session = _session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
