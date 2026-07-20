import os

import httpx
import pytest


@pytest.mark.edge_integration
def test_edge_backend_responds() -> None:
    base_url = os.environ.get("FAIRCOM_API_BASE_URL")
    assert base_url, "FAIRCOM_API_BASE_URL must be set for edge integration tests"

    # The backend may return 401/404 depending on endpoint and auth state.
    # Any non-5xx HTTP response confirms the container is reachable.
    response = httpx.get(base_url.rstrip("/") + "/", verify=False, timeout=5.0)
    assert response.status_code < 500
