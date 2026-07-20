from __future__ import annotations

from collections.abc import Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def create_health_app(readiness_check: Callable[[], bool] | None = None) -> Starlette:
    check = readiness_check or (lambda: True)

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def ready(_request: Request) -> JSONResponse:
        is_ready = bool(check())
        status_code = 200 if is_ready else 503
        status = "ready" if is_ready else "not_ready"
        return JSONResponse({"status": status}, status_code=status_code)

    return Starlette(
        routes=[
            Route("/health", endpoint=health),
            Route("/healthz", endpoint=health),
            Route("/ready", endpoint=ready),
            Route("/readyz", endpoint=ready),
        ]
    )
