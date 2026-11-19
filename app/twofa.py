"""TOTP two-factor authentication helpers."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets

import pyotp
import segno

ISSUER = "Odoo PaaS"
RECOVERY_CODE_COUNT = 10


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def qr_svg(uri: str) -> str:
    return segno.make(uri, error="m").svg_inline(scale=4, border=2)


def verify_code(secret: str, code: str) -> bool:
    digits = re.sub(r"\D", "", code or "")
    if not secret or len(digits) != 6:
        return False
    try:
        return pyotp.TOTP(secret).verify(digits, valid_window=1)
    except (TypeError, ValueError):
        return False


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list:
    codes = []
    for _ in range(count):
        raw = secrets.token_hex(5).upper()
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def _normalize_recovery_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(_normalize_recovery_code(code).encode()).hexdigest()


def hash_recovery_codes(codes: list) -> str:
    return json.dumps([hash_recovery_code(code) for code in codes])


def consume_recovery_code(stored: str, code: str) -> tuple:
    """Return (matched, remaining_hashes_json)."""
    target = hash_recovery_code(code)
    try:
        hashes = json.loads(stored or "[]")
    except (TypeError, ValueError):
        hashes = []
    for index, code_hash in enumerate(hashes):
        if hmac.compare_digest(str(code_hash), target):
            del hashes[index]
            return True, json.dumps(hashes)
    return False, stored
