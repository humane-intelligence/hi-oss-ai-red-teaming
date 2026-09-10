"""CORS preflight behaviour — `CORS_ORIGINS` is pinned to `http://localhost:3000` in conftest."""

import pytest
from fastapi import status
from fastapi.testclient import TestClient

_ALLOWED_ORIGIN = "http://localhost:3000"


@pytest.mark.integration
def test_preflight_allows_configured_origin(client: TestClient) -> None:
    response = client.options(
        "/health",
        headers={"Origin": _ALLOWED_ORIGIN, "Access-Control-Request-Method": "GET"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.headers["access-control-allow-origin"] == _ALLOWED_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"


@pytest.mark.integration
def test_preflight_omits_header_for_unknown_origin(client: TestClient) -> None:
    response = client.options(
        "/health",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"},
    )

    assert "access-control-allow-origin" not in response.headers
