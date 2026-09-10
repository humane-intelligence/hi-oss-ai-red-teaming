"""Integration tests for the `DataLicense` model's own column defaults."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.licenses.models import DataLicense


@pytest.mark.integration
async def test_a_new_licence_does_not_protect_conversation_data(db_session: AsyncSession) -> None:
    # The column is a deliberate opt-in: every licence that existed before this feature, and every one
    # created without stating a position, must leave exports and storage exactly as they were.
    lic = DataLicense(name="Acme Internal 1.0", short_description="Internal only", content="ACME …")
    db_session.add(lic)
    await db_session.flush()
    await db_session.refresh(lic)

    assert lic.protects_conversation_data is False
