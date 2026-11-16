"""Management CLI.

Usage (from the repository root, venv active as needed):

    python -m app.manage init
    python -m app.manage create-admin --email a@b.c --password secret [--role admin]
    python -m app.manage set-password --email a@b.c --password new
    python -m app.manage backup-all [--kind manual]
    python -m app.manage retention
    python -m app.manage doctor
"""
from __future__ import annotations

import argparse
import sys

from sqlmodel import select

from app.auth import hash_password
from app.config import settings
from app.db import init_db, session_scope
from app.models import Branch, User


def cmd_init(_args) -> None:
    init_db()
    print(f"state database ready: {settings.STATE_DB}")


def cmd_create_admin(args) -> None:
    init_db()
    with session_scope() as session:
        existing = session.exec(select(User).where(User.email == args.email.lower())).first()
        if existing:
            print(f"user {args.email} already exists")
            return
        session.add(
            User(
                email=args.email.lower(),
                name=args.name or "Admin",
                password_hash=hash_password(args.password),
                role=args.role,
            )
        )
    print(f"user {args.email} created with role {args.role}")


def cmd_set_password(args) -> None:
    with session_scope() as session:
        user = session.exec(select(User).where(User.email == args.email.lower())).first()
        if not user:
            print(f"user {args.email} not found")
            sys.exit(1)
        user.password_hash = hash_password(args.password)
        session.add(user)
    print("password updated")


def cmd_backup_all(args) -> None:
    from app.services import backups

    with session_scope() as session:
        branches = session.exec(select(Branch)).all()
    for branch in branches:
        try:
            backup = backups.create_backup(branch, kind=args.kind, note="cli")
            print(f"branch {branch.id} ({branch.name}): backup #{backup.id}")
        except Exception as exc:  # noqa: BLE001
            print(f"branch {branch.id} ({branch.name}): FAILED - {exc}")


def cmd_retention(_args) -> None:
    from app.services import backups

    with session_scope() as session:
        branches = session.exec(select(Branch)).all()
    total = 0
    for branch in branches:
        total += backups.run_retention(branch.id)
    print(f"deleted {total} backup(s)")


def cmd_doctor(_args) -> None:
    from app.services import docker_ctl

    print(f"app dir:       {settings.APP_DIR}")
    print(f"data dir:      {settings.DATA_DIR}")
    print(f"state db:      {settings.STATE_DB}")
    print(f"mock docker:   {settings.MOCK_DOCKER}")
    print(f"docker:        {'available' if docker_ctl.available() else 'NOT AVAILABLE'}")
    if not settings.MOCK_DOCKER:
        from app.services.docker_ctl import _docker_exec_pg

        result = _docker_exec_pg(["psql", "-U", settings.PG_USER, "-d", "postgres", "-c", "SELECT 1"])
        print(f"postgres:      {'ok' if result.returncode == 0 else 'ERROR'}")
    print(f"worker:        {'enabled' if settings.WORKER_ENABLED else 'disabled'}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.manage", description="Odoo PaaS management CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the state database").set_defaults(func=cmd_init)

    admin = sub.add_parser("create-admin", help="create a user")
    admin.add_argument("--email", required=True)
    admin.add_argument("--password", required=True)
    admin.add_argument("--name", default="")
    admin.add_argument("--role", default="admin", choices=["admin", "developer", "tester"])
    admin.set_defaults(func=cmd_create_admin)

    pw = sub.add_parser("set-password", help="reset a user's password")
    pw.add_argument("--email", required=True)
    pw.add_argument("--password", required=True)
    pw.set_defaults(func=cmd_set_password)

    backup = sub.add_parser("backup-all", help="back up every branch now")
    backup.add_argument("--kind", default="manual", choices=["manual", "auto", "update"])
    backup.set_defaults(func=cmd_backup_all)

    sub.add_parser("retention", help="apply backup retention").set_defaults(func=cmd_retention)
    sub.add_parser("doctor", help="print environment diagnostics").set_defaults(func=cmd_doctor)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
