"""Smoke tests for /health — also demonstrates the sync + async testing patterns."""

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from httpx import AsyncClient


@pytest.mark.integration
def test_health_sync_returns_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ok"}


@pytest.mark.integration
async def test_health_async_returns_ok(async_client: AsyncClient) -> None:
    response = await async_client.get("/health")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ok"}
