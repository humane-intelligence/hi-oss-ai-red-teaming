"""Shared fixtures for the ai_gateway tests."""

import pytest

from app.core.config import Settings
from app.core.config import get_settings


@pytest.fixture
def app_settings() -> Settings:
    return get_settings()
