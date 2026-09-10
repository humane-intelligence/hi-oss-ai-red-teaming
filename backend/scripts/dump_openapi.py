"""Dump the live FastAPI OpenAPI schema to ``docs/openapi.yaml``."""

from pathlib import Path

import yaml

from app.main import app

OUTPUT = Path(__file__).resolve().parent.parent / "docs" / "openapi.yaml"


def main() -> None:
    rendered = yaml.safe_dump(
        app.openapi(),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=120,
    )
    # Skip the write on identical content so pre-push hooks don't see a stat change.
    if OUTPUT.exists() and OUTPUT.read_text(encoding="utf-8") == rendered:
        return
    OUTPUT.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
