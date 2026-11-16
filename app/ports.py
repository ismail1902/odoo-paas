"""Host port allocation for branch builds.

Every branch owns exactly one port from ``PORT_RANGE_START..PORT_RANGE_END``.
Ports are persisted on the Branch row; allocation checks both the database
and the OS (bind test) to avoid collisions with unrelated services.
"""
from __future__ import annotations

import socket
from typing import Optional

from sqlmodel import Session, select

from app.config import settings
from app.errors import DeployError
from app.models import Branch


def is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def allocate_port(session: Session) -> int:
    taken = {
        row for row in session.exec(select(Branch.port)).all() if row
    }
    for port in range(settings.PORT_RANGE_START, settings.PORT_RANGE_END + 1):
        if port in taken:
            continue
        if is_port_free(port):
            return port
    raise DeployError(
        f"No free port in range {settings.PORT_RANGE_START}-{settings.PORT_RANGE_END}"
    )


def find_free_port(session: Session) -> Optional[int]:
    try:
        return allocate_port(session)
    except DeployError:
        return None
