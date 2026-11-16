"""Git repository operations and GitHub webhook verification."""
from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

from app.errors import BuildError


def _git(args: List[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env=env
    )


def branch_pushed(url: str, branch: str) -> bool:
    """True when the branch exists on the remote (empty repos have none)."""
    result = _git(["ls-remote", "--heads", url, f"refs/heads/{branch}"])
    if result.returncode != 0:
        raise BuildError(f"git ls-remote failed: {result.stderr.strip()}")
    return bool(result.stdout.strip())


def sync(
    repo: Path, url: str, branch: str, sha: Optional[str] = None
) -> Optional[str]:
    """Clone/update ``repo`` and check out ``sha`` (or origin/<branch>).

    Returns the checked-out HEAD sha, or ``None`` when the branch has no
    commits yet (empty repository / branch not pushed): the caller then
    deploys base Odoo without custom addons.
    """
    repo.parent.mkdir(parents=True, exist_ok=True)
    if not url.strip():
        if repo.exists():
            shutil.rmtree(repo, ignore_errors=True)
        return None
    if not sha and not branch_pushed(url, branch):
        if repo.exists():
            shutil.rmtree(repo, ignore_errors=True)
        return None
    if not (repo / ".git").exists():
        result = _git(["clone", "--branch", branch, "--single-branch", url, str(repo)])
        if result.returncode != 0:
            raise BuildError(f"git clone failed: {result.stderr.strip()}")
    else:
        _git(["remote", "set-url", "origin", url], cwd=repo)
        result = _git(["fetch", "origin", branch], cwd=repo)
        if result.returncode != 0:
            raise BuildError(f"git fetch failed: {result.stderr.strip()}")
    if sha:
        result = _git(["fetch", "origin", sha], cwd=repo)
        if result.returncode != 0:
            raise BuildError(f"git fetch failed: {result.stderr.strip()}")
        result = _git(["checkout", "--force", sha], cwd=repo)
        if result.returncode != 0:
            raise BuildError(f"git checkout failed: {result.stderr.strip()}")
        return head_sha(repo)
    result = _git(["checkout", "--force", "-B", branch, f"origin/{branch}"], cwd=repo)
    if result.returncode != 0:
        raise BuildError(f"git checkout failed: {result.stderr.strip()}")
    return head_sha(repo)


def head_sha(repo: Path) -> str:
    result = _git(["rev-parse", "HEAD"], cwd=repo)
    if result.returncode != 0:
        raise BuildError("git rev-parse failed")
    return result.stdout.strip()


def commit_info(repo: Path) -> dict:
    """Last commit metadata: sha, author, subject."""
    result = _git(["log", "-1", "--pretty=%H%x1f%an%x1f%s"], cwd=repo)
    if result.returncode != 0:
        return {"sha": "", "author": "", "message": ""}
    sha, author, subject = (result.stdout.strip().split("\x1f") + ["", "", ""])[:3]
    return {"sha": sha, "author": author, "message": subject}


def recent_commits(repo: Path, limit: int = 15) -> List[dict]:
    result = _git(
        ["log", f"-{limit}", "--pretty=%H%x1f%an%x1f%ad%x1f%s", "--date=short"],
        cwd=repo,
    )
    if result.returncode != 0:
        return []
    commits = []
    for line in result.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            commits.append(
                {"sha": parts[0][:10], "author": parts[1], "date": parts[2], "subject": parts[3]}
            )
    return commits


def verify_webhook(secret: str, body: bytes, signature_header: Optional[str]) -> bool:
    """Verify the X-Hub-Signature-256 header of a GitHub webhook request."""
    if not secret:
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature_header)
