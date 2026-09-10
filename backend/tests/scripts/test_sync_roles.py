"""Smoke test for the `scripts.sync_roles` deploy entrypoint.

The point of difference from `scripts.seed_local`: this entrypoint has NO
environment guard, so it must not refuse to run under ``ENVIRONMENT=test``
(the mode conftest pins for the suite). `sync_system_roles` itself is
exercised in `tests/core/auth/services/test_roles.py`; here we stub it out
and just verify the script wrapper runs to completion.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from scripts.sync_roles import sync_roles


@pytest.mark.integration
async def test_sync_roles_runs_under_test_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    async def _stub(_session: AsyncSession) -> dict[str, object]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr("scripts.sync_roles.sync_system_roles", _stub)

    await sync_roles()

    assert called, "wrapper must invoke sync_system_roles — no env guard in the way"
