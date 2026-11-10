from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .api import chatbot, static
from .core.heartbeat import collect_performance_metrics
from .core.logging_setup import (
    REQUEST_ID_HEADER,
    configure_logging,
    reset_request_id,
    set_request_id,
)
from .core.runtime_checks import run_startup_checks
from .core.settings import get_settings
from .services.storage.mongodb_storage import ThreadStorage
from .services.streaming.active_conversations import cleanup_idle

settings = get_settings()
logger = configure_logging(__name__)
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# ──────────────────────────────────────────────────────────────────────────────
# FastAPI app (skeleton)
# ──────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup (was @app.on_event("startup"))
    run_startup_checks(get_settings())
    app.state.thread_storage = await ThreadStorage.create()

    async def periodic_cleanup_task():
        while True:
            try:
                await asyncio.sleep(60)  # check every min
                # Storage is not needed here, conversation must have been saved when it was last used
                evicted = await cleanup_idle(
                    max_idle=timedelta(minutes=settings.CLEANUP_INTERVAL_MINS)
                )
                if evicted:
                    logger.info(
                        f"Evicted idle > {settings.CLEANUP_INTERVAL_MINS} mins: {evicted}"
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                # Don’t crash the task; log and continue
                logger.warning(f"Daily cleanup failed: {e}")

    async def performance_snapshot_task():
        while True:
            try:
                await app.state.thread_storage.save_performance_snapshot(
                    collect_performance_metrics()
                )
                await asyncio.sleep(settings.RUNTIME_METRICS_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Performance snapshot failed: {e}")
                await asyncio.sleep(settings.RUNTIME_METRICS_INTERVAL_SECONDS)

    # Launch background task
    app.state.periodic_cleanup = asyncio.create_task(periodic_cleanup_task())
    app.state.runtime_metrics = asyncio.create_task(performance_snapshot_task())

    try:
        yield
    finally:
        # Shutdown
        app.state.periodic_cleanup.cancel()
        app.state.runtime_metrics.cancel()
        await asyncio.gather(
            app.state.periodic_cleanup,
            app.state.runtime_metrics,
            return_exceptions=True,
        )
        await app.state.thread_storage.close()


app = FastAPI(
    title="ClimateClaw Backend",
    version=get_settings().VERSION,
    docs_url="/api/chatbot/docs",  # exposing FasAPI docs
    redoc_url="/api/chatbot/redoc",
    openapi_url="/api/chatbot/openapi.json",
    lifespan=lifespan,
)


# CORS – mirror the permissive defaults (might need to adjust later)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get(REQUEST_ID_HEADER)
    if not request_id or not REQUEST_ID_PATTERN.fullmatch(request_id):
        request_id = str(uuid4())

    token = set_request_id(request_id)
    try:
        # Handover the request to the rest of FastAPI, continue request
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
    finally:
        # Reset to prev value (token) to prevent leaking, clean-up
        reset_request_id(token)


# ──────────────────────────────────────────────────────────────────────────────
# Route registry
# ──────────────────────────────────────────────────────────────────────────────

app.include_router(static.router, prefix="/api/chatbot", tags=["static"])
app.include_router(chatbot.router, prefix="/api/chatbot", tags=["chatbot"])


@app.get("/healthz")
def _healthz():
    # Simple liveness probe
    return {"status": "ok", "version": get_settings().VERSION}
