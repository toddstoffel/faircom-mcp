import os

import httpx
import pytest

from faircom_mcp.api.client import FaircomAPIClient
from faircom_mcp.api.sql import SQLAdapter


@pytest.mark.edge_integration
def test_edge_backend_responds() -> None:
    base_url = os.environ.get("FAIRCOM_API_BASE_URL")
    assert base_url, "FAIRCOM_API_BASE_URL must be set for edge integration tests"

    # The backend may return 401/404 depending on endpoint and auth state.
    # Any non-5xx HTTP response confirms the container is reachable.
    response = httpx.get(base_url.rstrip("/") + "/", verify=False, timeout=5.0)
    assert response.status_code < 500


@pytest.mark.edge_integration
def test_sql_dry_run_preview_is_supported_by_edge_backend() -> None:
    base_url = os.environ.get("FAIRCOM_API_BASE_URL")
    username = os.environ.get("FAIRCOM_API_USERNAME", "ADMIN")
    password = os.environ.get("FAIRCOM_API_PASSWORD", "ADMIN")
    assert base_url, "FAIRCOM_API_BASE_URL must be set for edge integration tests"

    client = FaircomAPIClient(base_url=base_url, username=username, password=password)
    adapter = SQLAdapter(client)

    result = adapter.execute("SELECT 1", dry_run=True)

    assert result["mode"] == "dry_run"
    assert result["status"] == "success"
    assert result["statement_type"] == "UNKNOWN"
    assert result["would_succeed"] is True
