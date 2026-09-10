"""Unit tests for `scripts.dump_openapi`."""

import os
from pathlib import Path

import pytest
import yaml

from app.main import app
from scripts import dump_openapi


@pytest.mark.unit
def test_main_writes_yaml_matching_live_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "openapi.yaml"
    monkeypatch.setattr(dump_openapi, "OUTPUT", output)

    dump_openapi.main()

    assert yaml.safe_load(output.read_text(encoding="utf-8")) == app.openapi()


@pytest.mark.unit
def test_main_skips_write_on_identical_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "openapi.yaml"
    monkeypatch.setattr(dump_openapi, "OUTPUT", output)
    dump_openapi.main()
    os.utime(output, (0, 0))  # sentinel mtime — a rewrite would bump it

    dump_openapi.main()

    assert output.stat().st_mtime == 0
