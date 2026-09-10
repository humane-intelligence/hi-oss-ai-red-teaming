"""Tests for OpenAPI metadata helpers."""

import pytest

from app.core.openapi import app_version


@pytest.mark.unit
def test_app_version_returns_short_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_SHA", "831d547bf9671a1c")
    assert app_version() == "831d547"


@pytest.mark.unit
def test_app_version_falls_back_to_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_SHA", raising=False)
    assert app_version() == "dev"
