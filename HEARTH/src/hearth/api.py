"""Bounded authenticated API for a single gateway trust domain; loopback deployment by default."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.security import APIKeyHeader

from .engine import DomainError, Engine
from .models import Batch, CloseRequest, DemoRequest, ReadingBatch, Settings, digest
from .simulator import simulate
from .store import Store

LOGGER = logging.getLogger(__name__)
MAX_BODY_BYTES = 512 * 1024


class BoundedBody:
    """Bound chunked bodies before JSON parsing, not just Content-Length headers."""
    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Awaitable[Any]],
                       send: Callable[..., Awaitable[Any]]) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        async def secured_send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-content-type-options", b"nosniff"),
                    (b"cache-control", b"no-store"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; "
                     b"img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")]
            await send(message)
        body = bytearray()
        if scope["method"] in {"POST", "PUT", "PATCH"}:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > MAX_BODY_BYTES:
                    response = JSONResponse({"detail": "body limit exceeded"}, status_code=413)
                    await response(scope, receive, secured_send)
                    return
                if not message.get("more_body", False):
                    break
            replayed = False
            async def replay() -> dict[str, Any]:
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()
            await self.app(scope, replay, secured_send)
        else:
            await self.app(scope, receive, secured_send)


def create_app(database: str | Path, settings: Settings, keys: dict[str, str],
               clock: Callable[[], datetime] | None = None, watchdog: bool = True) -> FastAPI:
    if set(keys) != {"viewer", "ingest", "operator"} or any(len(v) < 32 for v in keys.values()):
        raise ValueError("three distinct viewer/ingest/operator keys of at least 32 characters required")
    if len(set(keys.values())) != 3:
        raise ValueError("role keys must be different")
    clock = clock or (lambda: datetime.now(UTC))
    store = Store(database, digest(settings.model_dump(mode="json")))
    engine = Engine(store, settings)
    health = {"watchdog": "starting" if watchdog else "disabled", "failures": 0}

    async def run_watchdog() -> None:
        while True:
            try:
                await asyncio.to_thread(engine.tick, clock())
                health["watchdog"] = "ok"
            except (sqlite3.Error, DomainError):
                health["watchdog"] = "fault"
                health["failures"] += 1
                LOGGER.exception("Edge watchdog failed; operator intervention required")
            await asyncio.sleep(1)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(run_watchdog()) if watchdog else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="HEARTH edge research API", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BoundedBody)
    app.state.engine, app.state.store, app.state.health = engine, store, health
    header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def require(*roles: str) -> Callable[..., None]:
        def dependency(token: str | None = Depends(header)) -> None:
            encoded = (token or "").encode()
            matches = [secrets.compare_digest(encoded, keys[role].encode()) for role in roles]
            if not any(matches):
                raise HTTPException(401, "missing or unauthorized API key")
        return dependency

    view = Depends(require("viewer", "operator"))
    ingest = Depends(require("ingest", "operator"))
    operator = Depends(require("operator"))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo raw inputs (including NaN, secrets, or huge payloads) in errors.
        errors = [{k: e[k] for k in ("loc", "msg", "type")} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.exception_handler(DomainError)
    async def domain_error(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(sqlite3.Error)
    async def storage_error(_: Request, exc: sqlite3.Error) -> JSONResponse:
        LOGGER.error("Storage unavailable: %s", type(exc).__name__)
        return JSONResponse({"detail": "storage unavailable; no write acknowledged"}, status_code=503)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"mode": settings.mode, **health, "safety_certified": False},
                            status_code=503 if health["watchdog"] == "fault" else 200)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(Path(__file__).with_name("dashboard.html"))

    @app.get("/assets/{name}", include_in_schema=False)
    def assets(name: str) -> FileResponse:
        if name not in {"dashboard.css", "dashboard.js"}:
            raise HTTPException(404)
        return FileResponse(Path(__file__).with_name(name))

    @app.get("/v1/openapi.json", dependencies=[view])
    def openapi() -> dict[str, Any]:
        return app.openapi()

    @app.post("/v1/batches", dependencies=[operator], status_code=201)
    def register(batch: Batch) -> dict[str, Any]:
        return {"data": engine.register(batch, clock()), "extensions": {}}

    @app.post("/v1/readings:batch", dependencies=[ingest])
    def readings(body: ReadingBatch) -> dict[str, Any]:
        return {"data": engine.ingest(body.readings, clock()), "extensions": {}}

    @app.get("/v1/stalls/{stall_id}/state", dependencies=[view])
    def stall_state(stall_id: str) -> dict[str, Any]:
        # Single-gateway trust domain; dedicated tenancy/RBAC is required before multi-market SaaS.
        with store.connection() as db:
            ids = [row[0] for row in db.execute("SELECT batch_id FROM batches WHERE stall_id=?", (stall_id,))]
        if not ids:
            raise HTTPException(404, "unknown stall")
        return {"data": [engine.state(bid, clock()) for bid in ids], "extensions": {}}

    @app.get("/v1/stalls/{stall_id}/batches/{batch_id}/shelf-life", dependencies=[view])
    def shelf_life(stall_id: str, batch_id: str) -> dict[str, Any]:
        result = engine.state(batch_id, clock())
        if result["stall_id"] != stall_id:
            raise HTTPException(404, "batch does not belong to stall")
        return {"data": result, "extensions": {}}

    @app.post("/v1/batches/{batch_id}:close", dependencies=[operator])
    def close(batch_id: str, body: CloseRequest) -> dict[str, Any]:
        return {"data": engine.close(batch_id, body.reason, clock()), "extensions": {}}

    @app.get("/v1/ledger/events", dependencies=[view])
    def events(after: Annotated[int, Query(ge=0)] = 0,
               limit: Annotated[int, Query(ge=1, le=1000)] = 100) -> dict[str, Any]:
        data = store.events(after, limit)
        return {"data": data, "next_after": data[-1]["seq"] if data else after, "extensions": {}}

    @app.get("/v1/ledger/verify", dependencies=[operator])
    def verify() -> dict[str, Any]:
        return {"data": store.verify(), "extensions": {}}

    @app.post("/v1/demo/run", dependencies=[operator])
    def demo(body: DemoRequest) -> dict[str, Any]:
        if settings.mode != "demo":
            raise HTTPException(403, "demo execution disabled in shadow deployment")
        return simulate(body, settings)

    return app
