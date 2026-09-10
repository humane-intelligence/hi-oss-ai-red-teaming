"""Smoke test for /version — the public deployed-version probe."""

import pytest
from fastapi import status
from httpx import AsyncClient


@pytest.mark.integration
async def test_version_returns_sha_and_environment(async_client: AsyncClient) -> None:
    response = await async_client.get("/version")

    assert response.status_code == status.HTTP_200_OK
    # No GIT_SHA in the test env → "dev"; conftest pins ENVIRONMENT=test.
    assert response.json() == {"version": "dev", "environment": "test"}
