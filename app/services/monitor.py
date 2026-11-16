"""Lightweight metrics: host (psutil) + branch container (docker stats)."""
from __future__ import annotations

import time

import psutil

from app.config import settings
from app.services import docker_ctl


def host_stats() -> dict:
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage(str(settings.DATA_DIR))
    try:
        load_1, load_5, load_15 = psutil.getloadavg()
    except (AttributeError, OSError):
        load_1 = load_5 = load_15 = 0.0
    return {
        # A short blocking sample keeps the first reading accurate too.
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "cpu_count": psutil.cpu_count(logical=True) or 1,
        "load_1": round(load_1, 2),
        "load_5": round(load_5, 2),
        "load_15": round(load_15, 2),
        "uptime": int(time.time() - psutil.boot_time()),
        "mem_used": memory.used,
        "mem_total": memory.total,
        "mem_percent": memory.percent,
        "swap_used": swap.used,
        "swap_total": swap.total,
        "swap_percent": swap.percent,
        "disk_used": disk.used,
        "disk_total": disk.total,
        "disk_percent": disk.percent,
    }


def branch_stats(branch) -> dict:
    return {
        "timestamp": time.time(),
        "host": host_stats(),
        "state": docker_ctl.container_state(docker_ctl.container_name(branch)),
        "container": docker_ctl.container_stats(branch),
    }
