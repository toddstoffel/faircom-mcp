import asyncio

import httpx

from faircom_mcp.transports.health import create_health_app


def _get(path: str, app: object) -> httpx.Response:
    async def _request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(path)

    return asyncio.run(_request())


def test_health_endpoint_returns_ok() -> None:
    app = create_health_app()
    response = _get("/health", app)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_endpoint_returns_503_when_not_ready() -> None:
    app = create_health_app(readiness_check=lambda: False)
    response = _get("/ready", app)

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_z_suffix_endpoints_remain_compatible() -> None:
    app = create_health_app(readiness_check=lambda: True)

    assert _get("/healthz", app).status_code == 200
    assert _get("/readyz", app).status_code == 200
