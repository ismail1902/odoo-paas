"""Application entrypoint.

Run locally (UI development, Docker simulated):
    uvicorn app.main:app --reload

Run in production (systemd unit in systemd/odoo-paas.service):
    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import api, web
from app.config import settings
from app.db import init_db
from app.worker import start_worker


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    start_worker()
    yield


def create_app() -> FastAPI:
    application = FastAPI(
        title=settings.APP_NAME,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    application.mount(
        "/static",
        StaticFiles(directory=str(settings.APP_DIR / "app" / "static")),
        name="static",
    )
    application.include_router(web.router)
    application.include_router(api.router)
    return application


app = create_app()
