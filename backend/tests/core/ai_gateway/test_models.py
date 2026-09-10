"""Integration tests for the `AiModel` DB constraints and soft-delete behaviour."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel


def _row(
    *,
    name: str = "Claude 3.5 Sonnet",
    model_alias: str = "claude-3-5-sonnet",
    provider: ProviderVendor = ProviderVendor.ANTHROPIC,
    provider_model_id: str = "claude-3-5-sonnet-20240620",
    description: str | None = None,
) -> AiModel:
    return AiModel(
        name=name,
        model_alias=model_alias,
        provider=provider,
        provider_model_id=provider_model_id,
        description=description,
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("provider_a", "provider_b"),
    [
        (ProviderVendor.ANTHROPIC, ProviderVendor.ANTHROPIC),
        (ProviderVendor.OPENAI, ProviderVendor.ANTHROPIC),
    ],
)
async def test_partial_unique_blocks_duplicate_live_name(
    db_session: AsyncSession, provider_a: ProviderVendor, provider_b: ProviderVendor
) -> None:
    db_session.add(_row(name="dup", model_alias="alias-a", provider=provider_a))
    await db_session.flush()
    db_session.add(_row(name="dup", model_alias="alias-b", provider=provider_b))

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.integration
async def test_partial_unique_blocks_duplicate_live_model_alias(db_session: AsyncSession) -> None:
    db_session.add(_row(name="alpha", model_alias="dup"))
    await db_session.flush()
    db_session.add(_row(name="beta", model_alias="dup"))

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.integration
async def test_partial_unique_allows_reusing_name_after_soft_delete(db_session: AsyncSession) -> None:
    first = _row(name="reusable", model_alias="alias-1")
    db_session.add(first)
    await db_session.flush()
    first.soft_delete(None)
    await db_session.flush()

    db_session.add(_row(name="reusable", model_alias="alias-2"))
    await db_session.flush()  # no IntegrityError — soft-deleted row is invisible to the partial index


@pytest.mark.integration
async def test_live_select_excludes_soft_deleted_rows(db_session: AsyncSession) -> None:
    keeper = _row(name="alive", model_alias="alive")
    tombstoned = _row(name="dead", model_alias="dead")
    db_session.add_all((keeper, tombstoned))
    await db_session.flush()
    tombstoned.soft_delete(None)
    await db_session.flush()

    result = await db_session.execute(AiModel.live_select())
    visible = {row.name for row in result.scalars().all()}

    assert visible == {"alive"}


@pytest.mark.integration
async def test_server_defaults_populate_parameters_extras_disabled_at(db_session: AsyncSession) -> None:
    """The row is created without explicitly setting the three columns so the DB defaults apply."""
    minimal = AiModel(
        name="defaults",
        model_alias="defaults",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )
    db_session.add(minimal)
    await db_session.flush()
    await db_session.refresh(minimal)

    assert minimal.parameters == {}
    assert minimal.extras == {}
    assert minimal.disabled_at is None
    assert minimal.is_disabled is False
    assert minimal.api_key_encrypted is None


@pytest.mark.integration
async def test_description_defaults_to_none_and_persists(db_session: AsyncSession) -> None:
    bare = _row(name="no-note", model_alias="no-note")
    noted = _row(
        name="noted",
        model_alias="noted",
        description="Client Acme only — do not assign elsewhere.",
    )
    db_session.add_all([bare, noted])
    await db_session.flush()
    await db_session.refresh(bare)
    await db_session.refresh(noted)

    assert bare.description is None
    assert noted.description == "Client Acme only — do not assign elsewhere."
