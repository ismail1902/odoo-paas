"""JSON + WebSocket endpoints.

- POST /webhooks/github/{project_id}  GitHub push hook (HMAC verified)
- GET  /healthz                       platform health
- GET  /api/monitor/{branch_id}       metrics as JSON (for agents/BI)
- WS   /ws/logs/{branch_id}           read-only live logs (container or build log)
- WS   /ws/shell/{branch_id}          interactive web shell (role-gated)
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from sqlmodel import select

from app.auth import can_open_shell, current_user_ws
from app.config import settings
from app.db import session_scope
from app.models import BUILD_QUEUED, Branch, Build, Event, Project
from app.services import docker_ctl, gitops, monitor

router = APIRouter()


# --- webhook ------------------------------------------------------------------


@router.post("/webhooks/github/{project_id}")
async def github_webhook(project_id: int, request: Request):
    body = await request.body()
    with session_scope() as session:
        project = session.get(Project, project_id)
        if project is None:
            return JSONResponse({"error": "unknown project"}, status_code=404)
        signature = request.headers.get("x-hub-signature-256")
        if not gitops.verify_webhook(project.webhook_secret, body, signature):
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        if request.headers.get("x-github-event") == "ping":
            return {"ok": True, "message": "webhook configured"}
        try:
            payload = json.loads(body.decode())
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "invalid payload"}, status_code=400)
        ref = payload.get("ref", "")
        if not ref.startswith("refs/heads/"):
            return {"skipped": "not a branch push"}
        branch_name = ref[len("refs/heads/"):]
        branch = session.exec(
            select(Branch).where(Branch.project_id == project_id, Branch.name == branch_name)
        ).first()
        if branch is None:
            return {"skipped": f"no branch '{branch_name}' configured"}
        after = payload.get("after") or ""
        head = payload.get("head_commit") or {}
        build = Build(
            branch_id=branch.id,
            status=BUILD_QUEUED,
            step="queued",
            commit_sha=after if after and set(after) != {"0"} else None,
            commit_message=(head.get("message") or "")[:200],
            commit_author=((head.get("author") or {}).get("name") or "")[:100],
        )
        session.add(build)
        branch.status = "building"
        session.add(branch)
        session.add(
            Event(
                project_id=project_id,
                branch_id=branch.id,
                kind="deploy",
                message=f"Push to '{branch_name}' queued a build",
            )
        )
        session.flush()
        build_id = build.id
    return JSONResponse({"queued": build_id}, status_code=202)


# --- health / metrics -------------------------------------------------------------


@router.get("/healthz")
def healthz():
    return {
        "ok": True,
        "app": settings.APP_NAME,
        "mock_docker": settings.MOCK_DOCKER,
        "docker": docker_ctl.available(),
    }


@router.get("/api/monitor/{branch_id}")
def api_monitor(branch_id: int, request: Request):
    with session_scope() as session:
        user = current_user_ws(request.cookies, session)
        if user is None:
            return {"error": "login required"}
        branch = session.get(Branch, branch_id)
        if branch is None:
            return {"error": "branch not found"}
        return monitor.branch_stats(branch)


# --- live logs (websocket) -----------------------------------------------------------


def _latest_build_log(branch_id: int) -> Optional[Path]:
    with session_scope() as session:
        build = session.exec(
            select(Build).where(Build.branch_id == branch_id).order_by(Build.id.desc())
        ).first()
        if build and build.log_path:
            return Path(build.log_path)
    return None


@router.websocket("/ws/logs/{branch_id}")
async def ws_logs(websocket: WebSocket, branch_id: int):
    await websocket.accept()
    with session_scope() as session:
        user = current_user_ws(websocket.cookies, session)
        branch = session.get(Branch, branch_id)
    if user is None:
        await websocket.close(code=4401, reason="login required")
        return
    if branch is None:
        await websocket.close(code=4404, reason="branch not found")
        return

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()

    container_running = docker_ctl.container_state(branch.container_name) == "running"
    build_log = None if container_running else _latest_build_log(branch_id)

    def pump() -> None:
        try:
            if container_running:
                for chunk in docker_ctl.stream_container_logs(branch):
                    if stop.is_set():
                        break
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            elif build_log is not None and build_log.exists():
                with build_log.open() as handle:
                    handle.seek(max(0, build_log.stat().st_size - 20000))
                    while not stop.is_set():
                        line = handle.readline()
                        if line:
                            loop.call_soon_threadsafe(queue.put_nowait, line)
                        else:
                            time.sleep(0.5)
            else:
                loop.call_soon_threadsafe(
                    queue.put_nowait, "(no running container and no build log yet)\n"
                )
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=pump, daemon=True).start()
    try:
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            await websocket.send_text(chunk)
    except WebSocketDisconnect:
        pass
    finally:
        stop.set()


# --- web shell (websocket) --------------------------------------------------------------


@router.websocket("/ws/shell/{branch_id}")
async def ws_shell(websocket: WebSocket, branch_id: int):
    await websocket.accept()
    with session_scope() as session:
        user = current_user_ws(websocket.cookies, session)
        branch = session.get(Branch, branch_id)
    if user is None:
        await websocket.close(code=4401, reason="login required")
        return
    if branch is None:
        await websocket.close(code=4404, reason="branch not found")
        return
    if not can_open_shell(user, branch.stage):
        await websocket.close(code=4403, reason=f"role '{user.role}' cannot open a {branch.stage} shell")
        return
    if docker_ctl.container_state(branch.container_name) != "running":
        await websocket.close(code=4409, reason="container is not running")
        return
    try:
        terminal = docker_ctl.Terminal(branch)
    except Exception as exc:  # noqa: BLE001
        await websocket.close(code=4500, reason=str(exc)[:120])
        return

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                data = terminal.read()
            except Exception:  # noqa: BLE001
                break
            if not data:
                break
            loop.call_soon_threadsafe(queue.put_nowait, data)
        loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=reader, daemon=True).start()

    async def sender() -> None:
        while True:
            item = await queue.get()
            if item is None:
                break
            await websocket.send_bytes(item)

    sender_task = asyncio.create_task(sender())
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                terminal.write(message["bytes"])
            elif message.get("text"):
                text = message["text"]
                try:
                    control = json.loads(text)
                except ValueError:
                    terminal.write(text.encode())
                    continue
                if control.get("type") == "resize":
                    terminal.resize(int(control.get("rows", 24)), int(control.get("cols", 80)))
    except WebSocketDisconnect:
        pass
    finally:
        stop.set()
        sender_task.cancel()
        terminal.close()
