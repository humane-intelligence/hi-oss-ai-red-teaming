"""Integration tests for `app.core.evaluations.services.evaluations` — CRUD + visibility over a real DB."""

import uuid
from datetime import UTC
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_gateway.enums import Modality
from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import grant_roles
from app.core.auth.roles import Permission
from app.core.config import get_settings
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import EvaluationStatus
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.filters import EvaluationFilters
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.schemas import EvaluationResponse
from app.core.evaluations.schemas import EvaluationUpdate
from app.core.evaluations.services.evaluations import authorize_evaluation_mutation
from app.core.evaluations.services.evaluations import create_evaluation
from app.core.evaluations.services.evaluations import get_evaluation
from app.core.evaluations.services.evaluations import list_evaluations
from app.core.evaluations.services.evaluations import resolve_effective_licenses
from app.core.evaluations.services.evaluations import soft_delete_evaluation
from app.core.evaluations.services.evaluations import update_evaluation
from app.core.exceptions import ForbiddenError
from app.core.exceptions import NotFoundError
from app.core.licenses.service import create_data_license
from app.core.licenses.service import get_default_license
from app.core.restore import restore_cutoff
from tests.conftest import persist_evaluation_group

_NO_FILTERS = EvaluationFilters()


# These suites exercise the live set; the window is inert but the services now
# require it explicitly, as the routes derive it.
_CUTOFF = restore_cutoff(get_settings())


async def _persist_evaluation(
    db_session: AsyncSession, *, mask: bool, group_id: uuid.UUID, created_by_id: uuid.UUID
) -> Evaluation:
    """Build an evaluation directly (not via the service) so its `models` relationship is never pre-loaded.

    A read in the same session would otherwise reuse the identity-mapped row with
    an already-loaded (empty) collection, masking the eager-load under test.
    """
    evaluation = Evaluation(
        title="t",
        description="d",
        evaluation_group_id=group_id,
        created_by_id=created_by_id,
        mask_models_enabled=mask,
    )
    db_session.add(evaluation)
    await db_session.flush()
    return evaluation


async def _persist_model(
    db_session: AsyncSession,
    *,
    model_alias: str = "assigned",
    warmup_enabled: bool = False,
    input_modalities: list[Modality] | None = None,
) -> AiModel:
    model = AiModel(
        name=f"Model {model_alias}",
        model_alias=model_alias,
        provider=ProviderVendor.ANTHROPIC,
        provider_model_id="claude-3-5-sonnet-20240620",
        warmup_enabled=warmup_enabled,
        input_modalities=input_modalities or [Modality.TEXT],
    )
    db_session.add(model)
    await db_session.flush()
    return model


async def _assign(
    db_session: AsyncSession, evaluation: Evaluation, model: AiModel, *, mask: str | None
) -> EvaluationAiModel:
    assignment = EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id, model_display_mask=mask)
    db_session.add(assignment)
    await db_session.flush()
    return assignment


@pytest.mark.integration
async def test_create_evaluation_starts_in_new(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)

    evaluation = await create_evaluation(
        db_session,
        title="Eval",
        description="d",
        evaluation_group_id=group.id,
        created_by_id=group.created_by_id,
        mask_models_enabled=False,
    )

    assert evaluation.status is EvaluationStatus.NEW
    assert evaluation.mask_models_enabled is False
    assert evaluation.created_by_id == group.created_by_id


@pytest.mark.integration
async def test_create_evaluation_forbidden_when_caller_not_group_owner(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.PUBLIC)

    with pytest.raises(ForbiddenError):
        await create_evaluation(
            db_session, title="t", description="d", evaluation_group_id=group.id, created_by_id=uuid.uuid4()
        )


@pytest.mark.integration
async def test_create_evaluation_private_group_not_owned_raises_not_found(db_session: AsyncSession) -> None:
    # A non-public group the caller can't see reads as missing — the gate never
    # leaks a private group's existence on create.
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)

    with pytest.raises(NotFoundError):
        await create_evaluation(
            db_session, title="t", description="d", evaluation_group_id=group.id, created_by_id=uuid.uuid4()
        )


@pytest.mark.integration
async def test_create_evaluation_manager_can_target_others_group(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    manager = User(email=f"{uuid.uuid4().hex[:8]}@example.com")
    db_session.add(manager)
    await db_session.flush()

    evaluation = await create_evaluation(
        db_session,
        title="t",
        description="d",
        evaluation_group_id=group.id,
        created_by_id=manager.id,
        can_manage=True,
    )

    assert evaluation.evaluation_group_id == group.id
    assert evaluation.created_by_id == manager.id


@pytest.mark.integration
async def test_authorize_evaluation_mutation_allows_parent_group_owner(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    evaluation = await create_evaluation(
        db_session, title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )

    # Owner: no raise.
    await authorize_evaluation_mutation(
        db_session,
        evaluation.id,
        caller_id=group.created_by_id,
        can_manage=False,
        permission=Permission.EVALUATIONS_UPDATE,
    )


@pytest.mark.integration
async def test_authorize_evaluation_mutation_forbids_non_owner_of_public_group(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.PUBLIC)
    evaluation = await create_evaluation(
        db_session, title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )

    with pytest.raises(ForbiddenError):
        await authorize_evaluation_mutation(
            db_session,
            evaluation.id,
            caller_id=uuid.uuid4(),
            can_manage=False,
            permission=Permission.EVALUATIONS_UPDATE,
        )


@pytest.mark.integration
async def test_authorize_evaluation_mutation_hides_non_owned_private_group(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    evaluation = await create_evaluation(
        db_session, title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )

    with pytest.raises(NotFoundError):
        await authorize_evaluation_mutation(
            db_session,
            evaluation.id,
            caller_id=uuid.uuid4(),
            can_manage=False,
            permission=Permission.EVALUATIONS_UPDATE,
        )


@pytest.mark.integration
async def test_authorize_evaluation_mutation_missing_evaluation_raises_not_found(db_session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await authorize_evaluation_mutation(
            db_session, uuid.uuid4(), caller_id=uuid.uuid4(), can_manage=False, permission=Permission.EVALUATIONS_UPDATE
        )


@pytest.mark.integration
async def test_authorize_evaluation_mutation_manager_allows_others_private_group(db_session: AsyncSession) -> None:
    # Manage lifts the owner/visibility gate: a manager may write an evaluation in
    # a private group it does not own.
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    evaluation = await create_evaluation(
        db_session, title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )

    await authorize_evaluation_mutation(
        db_session, evaluation.id, caller_id=uuid.uuid4(), can_manage=True, permission=Permission.EVALUATIONS_UPDATE
    )


@pytest.mark.integration
async def test_authorize_evaluation_mutation_manager_still_requires_existence(db_session: AsyncSession) -> None:
    # Manage lifts the owner/visibility gate but not existence — a missing
    # evaluation 404s even for a manager.
    with pytest.raises(NotFoundError):
        await authorize_evaluation_mutation(
            db_session, uuid.uuid4(), caller_id=uuid.uuid4(), can_manage=True, permission=Permission.EVALUATIONS_UPDATE
        )


@pytest.mark.integration
async def test_authorize_evaluation_mutation_manager_requires_live_group(db_session: AsyncSession) -> None:
    # Manage never lifts parent-group liveness: an evaluation under a soft-deleted
    # group is unwritable even for a manager.
    group = await persist_evaluation_group(db_session)
    evaluation = await create_evaluation(
        db_session, title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )
    group.soft_delete(None)
    await db_session.flush()

    with pytest.raises(NotFoundError):
        await authorize_evaluation_mutation(
            db_session,
            evaluation.id,
            caller_id=group.created_by_id,
            can_manage=True,
            permission=Permission.EVALUATIONS_UPDATE,
        )


@pytest.mark.integration
async def test_authorize_evaluation_mutation_allows_in_group_owner_non_creator(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # An added in-group `owner` (not the creator, no break-glass) may write the
    # group's evaluations — write authority comes from the object role.
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    evaluation = await create_evaluation(
        db_session, title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )
    member = User(email=f"{uuid.uuid4().hex[:8]}@example.com")
    db_session.add(member)
    await db_session.flush()
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group.id, member.id, [system_roles["owner"]])

    await authorize_evaluation_mutation(
        db_session, evaluation.id, caller_id=member.id, can_manage=False, permission=Permission.EVALUATIONS_UPDATE
    )


@pytest.mark.integration
async def test_authorize_evaluation_mutation_denies_member_with_lesser_role(
    db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # A member holding only red_teamer is denied even though they're a member —
    # the in-group role is authoritative (no evaluations:update in its grants).
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    evaluation = await create_evaluation(
        db_session, title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )
    member = User(email=f"{uuid.uuid4().hex[:8]}@example.com")
    db_session.add(member)
    await db_session.flush()
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group.id, member.id, [system_roles["red_teamer"]])

    with pytest.raises(ForbiddenError):
        await authorize_evaluation_mutation(
            db_session, evaluation.id, caller_id=member.id, can_manage=False, permission=Permission.EVALUATIONS_UPDATE
        )


@pytest.mark.integration
async def test_create_evaluation_unknown_group_raises_not_found(db_session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await create_evaluation(
            db_session, title="t", description="d", evaluation_group_id=uuid.uuid4(), created_by_id=uuid.uuid4()
        )


@pytest.mark.integration
async def test_create_evaluation_soft_deleted_group_raises_not_found(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    group.soft_delete(None)
    await db_session.flush()

    with pytest.raises(NotFoundError):
        await create_evaluation(
            db_session, title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
        )


@pytest.mark.integration
async def test_update_evaluation_applies_only_set_fields(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    evaluation = await create_evaluation(
        db_session,
        title="orig",
        description="d",
        evaluation_group_id=group.id,
        created_by_id=group.created_by_id,
        cover_image="cover.png",
    )

    updated = await update_evaluation(db_session, evaluation, EvaluationUpdate(title="renamed"))

    assert updated.title == "renamed"
    assert updated.cover_image == "cover.png"  # untouched — not in the change set


@pytest.mark.integration
async def test_update_evaluation_clears_cover_image_with_explicit_none(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    evaluation = await create_evaluation(
        db_session,
        title="t",
        description="d",
        evaluation_group_id=group.id,
        created_by_id=group.created_by_id,
        cover_image="cover.png",
    )

    updated = await update_evaluation(db_session, evaluation, EvaluationUpdate(cover_image=None))

    assert updated.cover_image is None


@pytest.mark.integration
async def test_soft_delete_evaluation_stamps_deleted_at(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    evaluation = await create_evaluation(
        db_session, title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )

    await soft_delete_evaluation(db_session, evaluation, by_id=uuid4())

    assert evaluation.is_deleted is True


@pytest.mark.integration
async def test_get_evaluation_detail_public_group_visible_to_anyone(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.PUBLIC)
    evaluation = await create_evaluation(
        db_session, title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )

    found = await get_evaluation(db_session, evaluation.id, caller_id=uuid.uuid4())

    assert found.id == evaluation.id


@pytest.mark.integration
async def test_get_evaluation_detail_private_group_hidden_from_non_owner(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    evaluation = await create_evaluation(
        db_session, title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )

    with pytest.raises(NotFoundError):
        await get_evaluation(db_session, evaluation.id, caller_id=uuid.uuid4())


@pytest.mark.integration
async def test_get_evaluation_detail_private_group_visible_to_owner(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    evaluation = await create_evaluation(
        db_session, title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )

    found = await get_evaluation(db_session, evaluation.id, caller_id=group.created_by_id)

    assert found.id == evaluation.id


@pytest.mark.integration
async def test_get_evaluation_detail_manager_sees_private_group(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    evaluation = await create_evaluation(
        db_session, title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )

    found = await get_evaluation(db_session, evaluation.id, caller_id=uuid.uuid4(), can_manage=True)

    assert found.id == evaluation.id


@pytest.mark.integration
async def test_get_evaluation_detail_manager_hidden_when_group_soft_deleted(db_session: AsyncSession) -> None:
    # Manage lifts the public-or-owned predicate, never parent-group liveness.
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    evaluation = await create_evaluation(
        db_session, title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )
    group.soft_delete(None)
    await db_session.flush()

    with pytest.raises(NotFoundError):
        await get_evaluation(db_session, evaluation.id, caller_id=uuid.uuid4(), can_manage=True)


@pytest.mark.integration
async def test_get_evaluation_detail_missing_raises_not_found(db_session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await get_evaluation(db_session, uuid.uuid4(), caller_id=uuid.uuid4())


@pytest.mark.integration
async def test_list_evaluations_filters_by_group(db_session: AsyncSession) -> None:
    group_a = await persist_evaluation_group(db_session, title="a")
    group_b = await persist_evaluation_group(db_session, title="b")
    await create_evaluation(
        db_session, title="in-a", description="d", evaluation_group_id=group_a.id, created_by_id=group_a.created_by_id
    )
    await create_evaluation(
        db_session, title="in-b", description="d", evaluation_group_id=group_b.id, created_by_id=group_b.created_by_id
    )

    items, total = await list_evaluations(
        db_session,
        caller_id=uuid.uuid4(),
        filters=EvaluationFilters(evaluation_group_id=group_a.id),
        order_by="-created_at",
        limit=50,
        offset=0,
        deleted_cutoff=_CUTOFF,
    )

    assert total == 1
    assert items[0].title == "in-a"


@pytest.mark.integration
async def test_list_evaluations_hides_private_group_from_non_owner(db_session: AsyncSession) -> None:
    public = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.PUBLIC)
    private = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    await create_evaluation(
        db_session, title="pub", description="d", evaluation_group_id=public.id, created_by_id=public.created_by_id
    )
    await create_evaluation(
        db_session, title="priv", description="d", evaluation_group_id=private.id, created_by_id=private.created_by_id
    )

    items, total = await list_evaluations(
        db_session,
        caller_id=uuid.uuid4(),
        filters=_NO_FILTERS,
        order_by="-created_at",
        limit=50,
        offset=0,
        deleted_cutoff=_CUTOFF,
    )

    assert total == 1
    assert items[0].title == "pub"


@pytest.mark.integration
async def test_list_evaluations_manager_sees_private_group(db_session: AsyncSession) -> None:
    public = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.PUBLIC)
    private = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    await create_evaluation(
        db_session, title="pub", description="d", evaluation_group_id=public.id, created_by_id=public.created_by_id
    )
    await create_evaluation(
        db_session, title="priv", description="d", evaluation_group_id=private.id, created_by_id=private.created_by_id
    )

    items, total = await list_evaluations(
        db_session,
        caller_id=uuid.uuid4(),
        can_manage=True,
        filters=_NO_FILTERS,
        order_by="-created_at",
        limit=50,
        offset=0,
        deleted_cutoff=_CUTOFF,
    )

    assert total == 2
    assert {item.title for item in items} == {"pub", "priv"}


@pytest.mark.integration
async def test_list_evaluations_manager_excludes_soft_deleted_group(db_session: AsyncSession) -> None:
    # Even with manage, evaluations under a soft-deleted group are hidden from everyone.
    live = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    gone = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    await create_evaluation(
        db_session, title="live", description="d", evaluation_group_id=live.id, created_by_id=live.created_by_id
    )
    await create_evaluation(
        db_session, title="gone", description="d", evaluation_group_id=gone.id, created_by_id=gone.created_by_id
    )
    gone.soft_delete(None)
    await db_session.flush()

    items, total = await list_evaluations(
        db_session,
        caller_id=uuid.uuid4(),
        can_manage=True,
        filters=_NO_FILTERS,
        order_by="-created_at",
        limit=50,
        offset=0,
        deleted_cutoff=_CUTOFF,
    )

    assert total == 1
    assert {item.title for item in items} == {"live"}


@pytest.mark.integration
async def test_list_evaluations_search_matches_title(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    await create_evaluation(
        db_session,
        title="jailbreak gauntlet",
        description="d",
        evaluation_group_id=group.id,
        created_by_id=group.created_by_id,
    )
    await create_evaluation(
        db_session,
        title="benign probe",
        description="d",
        evaluation_group_id=group.id,
        created_by_id=group.created_by_id,
    )

    items, total = await list_evaluations(
        db_session,
        caller_id=uuid.uuid4(),
        filters=EvaluationFilters(search="gauntlet"),
        order_by="-created_at",
        limit=50,
        offset=0,
        deleted_cutoff=_CUTOFF,
    )

    assert total == 1
    assert items[0].title == "jailbreak gauntlet"


@pytest.mark.integration
async def test_masking_off_surfaces_real_model_fields(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    evaluation = await _persist_evaluation(db_session, mask=False, group_id=group.id, created_by_id=group.created_by_id)
    model = await _persist_model(db_session, warmup_enabled=True, input_modalities=[Modality.TEXT, Modality.IMAGE])
    assignment = await _assign(db_session, evaluation, model, mask="Hidden")

    detail = await get_evaluation(db_session, evaluation.id, caller_id=uuid.uuid4())
    lic = await get_default_license(db_session)
    view = EvaluationResponse.from_model(detail, effective_license=lic).models[0]

    assert view.assignment_id == assignment.id
    assert view.name == model.name
    assert view.provider == ProviderVendor.ANTHROPIC
    assert view.provider_model_id == "claude-3-5-sonnet-20240620"
    assert view.warmup_enabled is True
    assert view.input_modalities == [Modality.TEXT, Modality.IMAGE]


@pytest.mark.integration
async def test_masking_on_hides_model_identity(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    evaluation = await _persist_evaluation(db_session, mask=True, group_id=group.id, created_by_id=group.created_by_id)
    model = await _persist_model(db_session, warmup_enabled=True, input_modalities=[Modality.TEXT, Modality.IMAGE])
    model.parameters = {"temperature": 0.5, "system_prompt": "You are Claude by Anthropic."}
    assignment = await _assign(db_session, evaluation, model, mask="Model A")
    assignment.parameters = {"top_p": 0.9}
    await db_session.flush()

    detail = await get_evaluation(db_session, evaluation.id, caller_id=uuid.uuid4())
    lic = await get_default_license(db_session)
    view = EvaluationResponse.from_model(detail, effective_license=lic).models[0]

    assert view.assignment_id == assignment.id
    assert view.name == "Model A"
    assert view.provider is None
    assert view.provider_model_id is None
    # warmup_enabled + input_modalities are capability signals, not identifiers — surfaced even under masking.
    assert view.warmup_enabled is True
    assert view.input_modalities == [Modality.TEXT, Modality.IMAGE]
    # Numeric knobs cross the mask; the identity-revealing system_prompt is withheld.
    assert view.effective_parameters == {"temperature": 0.5, "top_p": 0.9}
    assert view.advanced_params_disabled is False


@pytest.mark.integration
async def test_view_withholds_effective_parameters_when_model_opts_out(db_session: AsyncSession) -> None:
    """A cascade-opted-out model must not advertise inheritable params it will never send."""
    group = await persist_evaluation_group(db_session)
    evaluation = await _persist_evaluation(db_session, mask=False, group_id=group.id, created_by_id=group.created_by_id)
    model = await _persist_model(db_session)
    model.parameters = {"temperature": 0.5}
    model.advanced_params_disabled = True
    assignment = await _assign(db_session, evaluation, model, mask=None)
    assignment.parameters = {"top_p": 0.9}
    await db_session.flush()

    detail = await get_evaluation(db_session, evaluation.id, caller_id=uuid.uuid4())
    lic = await get_default_license(db_session)
    view = EvaluationResponse.from_model(detail, effective_license=lic).models[0]

    assert view.advanced_params_disabled is True
    assert view.effective_parameters == {}


@pytest.mark.integration
async def test_read_excludes_soft_deleted_assignment(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    evaluation = await _persist_evaluation(db_session, mask=False, group_id=group.id, created_by_id=group.created_by_id)
    model = await _persist_model(db_session)
    assignment = await _assign(db_session, evaluation, model, mask=None)
    assignment.soft_delete(None)
    await db_session.flush()

    detail = await get_evaluation(db_session, evaluation.id, caller_id=uuid.uuid4())

    assert detail.models == []


@pytest.mark.integration
async def test_get_evaluation_ignores_visibility(db_session: AsyncSession) -> None:
    """The mutation-path loader resolves a private-group evaluation for any caller — access is gated upstream."""
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    evaluation = await create_evaluation(
        db_session, title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )

    found = await get_evaluation(db_session, evaluation.id)

    assert found.id == evaluation.id


@pytest.mark.integration
async def test_get_evaluation_missing_raises_not_found(db_session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await get_evaluation(db_session, uuid.uuid4())


@pytest.mark.integration
async def test_resolve_effective_licenses_still_resolves_a_soft_deleted_referenced_license(
    db_session: AsyncSession,
) -> None:
    # License lineage doesn't lapse: a tombstoned licence still resolves for the rows that
    # reference it. The resolver uses a plain select (not `live_select`) on purpose — this test
    # guards that invariant, so a future switch to a live filter fails loudly here instead of
    # silently dropping referenced-but-deleted licences back to the platform default.
    group = await persist_evaluation_group(db_session)
    assert group.created_by_id is not None
    lic = await create_data_license(
        db_session,
        caller_id=group.created_by_id,
        name="Doomed 1.0",
        version=None,
        short_description="soon tombstoned",
        content="TEXT",
        reference_url=None,
    )
    evaluation = await _persist_evaluation(db_session, mask=True, group_id=group.id, created_by_id=group.created_by_id)
    evaluation.data_license_id = lic.id
    db_session.add(evaluation)
    await db_session.flush()

    lic.soft_delete(None)
    db_session.add(lic)
    await db_session.flush()

    resolved = await resolve_effective_licenses(db_session, [evaluation.id])

    assert resolved[evaluation.id].id == lic.id
    assert resolved[evaluation.id].deleted_at is not None


@pytest.mark.integration
async def test_cascade_resolver_does_not_load_the_license_text(db_session: AsyncSession) -> None:
    # The hottest licence read in the app: every evaluation / conversation / conversation-group list
    # and every export resolves the cascade. The projections it feeds carry `has_content`, never the
    # text, and a curated legal text runs to tens of KB — so the column stays behind.
    owner = User(email="cascade-lic@example.com", hashed_password="x", is_active=True)
    db_session.add(owner)
    await db_session.flush()
    group = EvaluationGroup(
        title="G",
        description="d",
        access_level=EvaluationGroupAccessLevel.PUBLIC,
        start_date=datetime.now(UTC).date(),
        created_by_id=owner.id,
        status=PublicationStatus.APPROVED,
    )
    db_session.add(group)
    await db_session.flush()
    evaluation = Evaluation(title="E", description="d", evaluation_group_id=group.id, created_by_id=owner.id)
    db_session.add(evaluation)
    await db_session.flush()

    resolved = await resolve_effective_licenses(db_session, [evaluation.id])

    lic = resolved[evaluation.id]
    state = sa_inspect(lic)
    assert state is not None
    assert "content" in state.unloaded
    assert lic.has_text is not None  # the flag still answers, straight from SQL
    with pytest.raises(InvalidRequestError, match="raiseload"):
        _ = lic.content
