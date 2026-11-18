"""PostgreSQL/pgvector versions and their Odoo compatibility.

All supported versions use pgvector images so Odoo 19/20 AI can create the
``vector`` extension. The shared containers and volumes are provisioned by
``install.sh``; the platform never moves a branch database between versions.
Every Odoo version 15–20 was tested against PostgreSQL 17 and 18 with base,
mail, common business modules, an update and a web login.
"""
from __future__ import annotations

from app.config import settings

# Newest first. Add a version here only after provisioning its container.
VERSIONS = ("18", "17", "16", "15")

DEFAULT_VERSION = "16"

# Inclusive Odoo major range. All four versions meet the documented Odoo
# minimums; Odoo 20 requires PostgreSQL 16+. PG15 is not offered for Odoo 20.
ODOO_COMPATIBILITY = {
    "15": (15, 19),
    "16": (15, 99),
    "17": (15, 99),
    "18": (15, 99),
}


def odoo_major(value: str) -> int:
    try:
        return int((value or "").split(".")[0])
    except ValueError:
        return 0


def normalize(value: str) -> str:
    value = (value or "").strip()
    if value in VERSIONS:
        return value
    major = value.split(".")[0] if value else ""
    return major if major in VERSIONS else DEFAULT_VERSION


def compatible_versions(odoo_version: str) -> list:
    major = odoo_major(odoo_version)
    result = []
    for version in VERSIONS:
        minimum, maximum = ODOO_COMPATIBILITY[version]
        if minimum <= major <= maximum:
            result.append(version)
    return result


def is_compatible(pg_version: str, odoo_version: str) -> bool:
    return normalize(pg_version) in compatible_versions(odoo_version)


def version_for(record) -> str:
    return normalize(getattr(record, "postgres_version", "") or "")


def container_name(version: str) -> str:
    version = normalize(version)
    if version == "18":
        return settings.PG_CONTAINER_18
    if version == "17":
        return settings.PG_CONTAINER_17
    if version == "16":
        return settings.PG_CONTAINER
    return settings.PG_CONTAINER_15


def label(version: str) -> str:
    return f"PostgreSQL {normalize(version)} (pgvector)"


def default_version(odoo_version: str, available: list) -> str:
    if DEFAULT_VERSION in available and is_compatible(DEFAULT_VERSION, odoo_version):
        return DEFAULT_VERSION
    for version in VERSIONS:
        if version in available and is_compatible(version, odoo_version):
            return version
    return available[0] if available else DEFAULT_VERSION


def options(odoo_versions: list, available: list) -> list:
    result = []
    for version in VERSIONS:
        if version not in available:
            continue
        compatible = [v for v in odoo_versions if is_compatible(version, v)]
        if compatible:
            result.append(
                {
                    "version": version,
                    "label": label(version),
                    "odoo_versions": compatible,
                }
            )
    return result
