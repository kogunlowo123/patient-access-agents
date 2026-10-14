"""FastAPI application factory: REST API, MCP server, A2A endpoint and background workers."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from agentic_core.observability import bind_correlation_id, get_logger
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings

from patient_access_agents import __version__
from patient_access_agents.api.a2a import router as a2a_router
from patient_access_agents.api.mcp import build_mcp
from patient_access_agents.api.routes import router
from patient_access_agents.domain.errors import AccessError
from patient_access_agents.runtime.container import Container

ContainerFactory = Callable[[], Awaitable[Container]]

log = get_logger("patient_access_agents.api")


async def run_workers_forever(
    container: Container,
    interval_s: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Eligibility retries, reminder delivery and SLA evaluation, once per interval."""
    while True:
        try:
            summary = await container.service.run_due_work()
            container.metrics.worker_runs.labels("ok").inc()
            if summary.eligibility_retried or summary.reminders_processed:
                log.info("work_cycle", **summary.model_dump())
        except Exception:
            container.metrics.worker_runs.labels("failed").inc()
            log.exception("work_cycle_failed")
        await sleep(interval_s)


def create_app(factory: ContainerFactory) -> FastAPI:
    holder: dict[str, Container] = {}

    def current() -> Container:
        return holder["container"]

    mcp_server = build_mcp(current)
    # Stateless JSON mode: no session affinity, any replica serves any call.
    # Every call is authenticated with a bearer token, so DNS-rebinding
    # protection (meant for unauthenticated localhost servers) is not used.
    mcp_app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = await factory()
        holder["container"] = container
        app.state.container = container
        workers: asyncio.Task[None] | None = None
        async with mcp_server.session_manager.run():
            if container.settings.workers_enabled:
                workers = asyncio.create_task(
                    run_workers_forever(container, container.settings.worker_interval_s)
                )
            log.info("startup", version=__version__)
            try:
                yield
            finally:
                if workers is not None:
                    workers.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await workers
                await container.aclose()

    app = FastAPI(
        title="Patient Access Agents",
        version=__version__,
        description=(
            "Referral intake (FHIR ServiceRequest), eligibility verification (X12 270/271), "
            "constraint-based scheduling against FHIR Slots, follow-up reminders and escalation "
            "to human queues. Also exposed as MCP tools at /mcp and over A2A at /a2a."
        ),
        lifespan=lifespan,
    )
    app.include_router(router)
    app.include_router(a2a_router)
    app.router.routes.extend(mcp_app.routes)

    @app.middleware("http")
    async def correlation_and_metrics(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-correlation-id")
        cid = incoming if incoming and len(incoming) <= 64 and incoming.isprintable() else None
        started = time.perf_counter()
        with bind_correlation_id(cid) as bound:
            response = await call_next(request)
        response.headers["X-Correlation-ID"] = bound
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        container: Container | None = getattr(request.app.state, "container", None)
        if container is not None:
            container.metrics.requests.labels(
                request.method, path, str(response.status_code)
            ).observe(time.perf_counter() - started)
        return response

    @app.exception_handler(AccessError)
    async def access_error(_: Request, exc: AccessError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse(
            status_code=exc.status,
            content={"error": exc.code, "error_description": exc.message},
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_request",
                "error_description": "request failed validation",
                "details": [
                    {"loc": list(e.get("loc", ())), "msg": str(e.get("msg", ""))}
                    for e in exc.errors()
                ],
            },
        )

    return app
