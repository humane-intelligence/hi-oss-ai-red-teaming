"""Tests for the Prometheus /metrics endpoint."""

import pytest
from fastapi import status
from httpx import AsyncClient

from app.main import app


@pytest.mark.integration
async def test_metrics_exposes_red_metrics_after_a_request(async_client: AsyncClient) -> None:
    await async_client.get("/version")

    response = await async_client.get("/metrics")

    assert response.status_code == status.HTTP_200_OK
    assert "http_requests_total" in response.text
    assert "http_request_duration_seconds" in response.text


@pytest.mark.integration
async def test_metrics_excludes_probe_handlers(async_client: AsyncClient) -> None:
    await async_client.get("/health")

    response = await async_client.get("/metrics")

    assert 'handler="/health"' not in response.text


@pytest.mark.unit
def test_metrics_route_is_not_in_openapi_schema() -> None:
    assert "/metrics" not in app.openapi()["paths"]
