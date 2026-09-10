"""Integration tests for the `/v1/ai-models` router."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from uuid import UUID
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import status
from httpx import AsyncClient
from httpx import Response
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.api.v1 import ai_models as ai_models_route
from app.core.ai_gateway.crypto import decrypt_secret
from app.core.ai_gateway.enums import HealthCheckStatus
from app.core.ai_gateway.enums import Modality
from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import PROBED_CALL_FIELDS
from app.core.ai_gateway.models import AiModel
from app.core.ai_gateway.services.ai_models import create_model as create_model_service
from app.core.ai_gateway.services.health import start_health_check as start_health_check_service
from app.core.audit.service import changed_fields
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.roles import Permission
from app.core.auth.services.users import create_user as create_user_service
from app.core.config import get_settings
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationGroupAiModel
from app.core.evaluations.schemas import EvaluationAiModelResponse
from app.core.evaluations.schemas import EvaluationAiModelView
from tests.api.v1.conftest import caller_with
from tests.api.v1.conftest import make_token as _token
from tests.conftest import persist_evaluation_group


@pytest_asyncio.fixture
async def admin_role(db_session: AsyncSession) -> Role:
    role = Role(
        name="admin",
        description="Admin role for model CRUD tests",
        permissions=[
            Permission.MODELS_READ.value,
            Permission.MODELS_CREATE.value,
            Permission.MODELS_UPDATE.value,
            Permission.MODELS_DELETE.value,
        ],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def reader_role(db_session: AsyncSession) -> Role:
    role = Role(name="reader", description="Read-only", permissions=[Permission.MODELS_READ.value])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


def _create_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "Claude 3.5 Sonnet",
        "model_alias": "claude-3-5-sonnet",
        "provider": "anthropic",
        "provider_model_id": "claude-3-5-sonnet-20240620",
    }
    payload.update(overrides)
    return payload


def _bulk_row(key: str, **overrides: object) -> dict[str, object]:
    return {"row_key": key, "data": _create_payload(**overrides)}


def _key_row(key: str, *, name: str, api_key: str) -> dict[str, object]:
    return {"row_key": key, "data": {"name": name, "api_key": api_key}}


@pytest.mark.integration
async def test_create_model_returns_201_and_location(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="creator@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models",
        json=_create_payload(),
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["name"] == "Claude 3.5 Sonnet"
    assert body["model_alias"] == "claude-3-5-sonnet"
    assert body["provider"] == "anthropic"
    assert body["input_modalities"] == ["text"]
    assert body["output_modalities"] == ["text"]
    assert body["is_disabled"] is False
    assert body["warmup_enabled"] is False
    assert body["advanced_params_disabled"] is False
    assert body["has_api_key"] is False
    # When the caller omits `parameters`, the row stores an empty dict — not
    # `{"system_prompt": null, "temperature": null, …}`.
    assert body["parameters"] == {}
    assert response.headers["Location"] == f"/api/v1/ai-models/{body['id']}"


@pytest.mark.integration
async def test_create_stores_and_returns_the_description(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="describer@example.com", roles=[admin_role])
    note = "Client Acme only — do not assign elsewhere."

    response = await auth_db_client.post(
        "/api/v1/ai-models",
        json=_create_payload(description=note),
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["description"] == note
    stored = await db_session.get(AiModel, UUID(response.json()["id"]))
    assert stored is not None
    assert stored.description == note


@pytest.mark.integration
async def test_create_without_a_description_stores_none(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="noteless@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models",
        json=_create_payload(),
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["description"] is None
    stored = await db_session.get(AiModel, UUID(response.json()["id"]))
    assert stored is not None
    assert stored.description is None


@pytest.mark.integration
async def test_bulk_create_stores_the_description(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    """The bulk route reuses `AiModelCreate` through its own processor, so it is a second write path.

    Asserting the stored value is what catches a processor that accepted the field and dropped it.
    """
    caller = await create_user_service(db_session, email="bulk-describer@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models/bulk",
        json={"rows": [_bulk_row("r1", name="Bulk noted", model_alias="bulk-noted", description="Bulk-seeded note.")]},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    created = response.json()["results"][0]
    assert created["error"] is None
    assert created["data"]["description"] == "Bulk-seeded note."
    stored = await db_session.get(AiModel, UUID(created["data"]["id"]))
    assert stored is not None
    assert stored.description == "Bulk-seeded note."


@pytest.mark.integration
async def test_list_returns_the_description(
    auth_db_client: AsyncClient, db_session: AsyncSession, reader_role: Role
) -> None:
    """The listing builds its own response objects per row, so the field reaching them is its own fact."""
    caller = await create_user_service(db_session, email="lister@example.com", roles=[reader_role])
    model = await _persist_model(db_session, "listed-note")
    model.description = "Client Acme only."
    await db_session.flush()

    response = await auth_db_client.get(
        "/api/v1/ai-models",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    listed = next(item for item in response.json()["items"] if item["id"] == str(model.id))
    assert listed["description"] == "Client Acme only."


@pytest.mark.integration
async def test_detail_returns_the_description(
    auth_db_client: AsyncClient, db_session: AsyncSession, reader_role: Role
) -> None:
    """The route behind the model profile — a narrowed projection there shows `—` for every model."""
    caller = await create_user_service(db_session, email="detail-note@example.com", roles=[reader_role])
    model = await _persist_model(db_session, "detail-note")
    model.description = "Client Acme only."
    db_session.add(model)
    await db_session.flush()

    response = await auth_db_client.get(
        f"/api/v1/ai-models/{model.id}", headers={"Authorization": f"Bearer {_token(caller)}"}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["description"] == "Client Acme only."


@pytest.mark.unit
def test_description_is_absent_from_the_assignment_projection() -> None:
    """The assignment sub-resource's own response carries no note — masking is `EvaluationAiModelView`'s job."""
    assert "description" not in EvaluationAiModelResponse.model_fields


@pytest.mark.unit
def test_labels_are_absent_from_both_assignment_projections() -> None:
    """Labels never reach a red-teamer, masked or not — they are free text that can name the model.

    Stronger than the note's exclusion, which only relies on the masked view: a label like
    `llama-3-70b-box` defeats `mask_models_enabled` outright, so neither projection may carry it.
    """
    assert "labels" not in EvaluationAiModelView.model_fields
    assert "labels" not in EvaluationAiModelResponse.model_fields


@pytest.mark.integration
async def test_patch_sets_the_description(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="patch-set-note@example.com", roles=[admin_role])
    model = await _persist_model(db_session, "patch-set-note")
    assert model.description is None  # a different starting value, so the write can genuinely fail
    model_id = model.id

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{model_id}",
        json={"description": "Now Globex."},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["description"] == "Now Globex."
    db_session.expire_all()
    stored = await db_session.get(AiModel, model_id)
    assert stored is not None
    assert stored.description == "Now Globex."


@pytest.mark.integration
async def test_patch_with_explicit_null_clears_the_description(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    """Unlike the fields backing NOT NULL columns, this one takes an explicit `null` and clears."""
    caller = await create_user_service(db_session, email="patch-clear-note@example.com", roles=[admin_role])
    model = await _persist_model(db_session, "patch-clear-note")
    model.description = "Existing note."
    await db_session.flush()
    model_id = model.id

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{model_id}",
        json={"description": None},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["description"] is None
    db_session.expire_all()
    stored = await db_session.get(AiModel, model_id)
    assert stored is not None
    assert stored.description is None


@pytest.mark.integration
async def test_patch_omitting_the_description_leaves_it_unchanged(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="patch-omit-note@example.com", roles=[admin_role])
    model = await _persist_model(db_session, "patch-omit-note")
    model.description = "Keep me."
    await db_session.flush()
    model_id = model.id

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{model_id}",
        json={"name": "Renamed model"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["description"] == "Keep me."
    db_session.expire_all()
    stored = await db_session.get(AiModel, model_id)
    assert stored is not None
    assert stored.description == "Keep me."


@pytest.mark.integration
async def test_patch_with_a_blank_description_clears_it(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    """Blank is not a note: the column holds one form of "unset", so readers need no trim."""
    caller = await create_user_service(db_session, email="patch-blank-note@example.com", roles=[admin_role])
    model = await _persist_model(db_session, "patch-blank-note")
    model.description = "Client Acme only."
    await db_session.flush()
    model_id = model.id

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{model_id}",
        json={"description": "   "},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["description"] is None
    db_session.expire_all()
    stored = await db_session.get(AiModel, model_id)
    assert stored is not None
    assert stored.description is None


@pytest.mark.integration
async def test_create_trims_the_description_and_drops_a_blank_one(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    """Surrounding whitespace of a pasted note is noise, mirroring `inference_endpoint`."""
    caller = await create_user_service(db_session, email="create-trim-note@example.com", roles=[admin_role])

    padded = await auth_db_client.post(
        "/api/v1/ai-models",
        json=_create_payload(name="Padded", model_alias="padded", description="  Client Acme only.  "),
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )
    blank = await auth_db_client.post(
        "/api/v1/ai-models",
        json=_create_payload(name="Blank", model_alias="blank", description="   "),
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert padded.status_code == status.HTTP_201_CREATED
    assert padded.json()["description"] == "Client Acme only."
    assert blank.status_code == status.HTTP_201_CREATED
    assert blank.json()["description"] is None


@pytest.mark.integration
async def test_create_model_rejects_generic_without_inference_endpoint(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="urlless-creator@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models",
        json=_create_payload(provider="generic"),
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == status.HTTP_400_BAD_REQUEST
    assert body["title"] == "Bad Request"
    # Field-addressable so the console maps it inline onto the form input.
    assert body["errors"][0]["loc"] == ["body", "inference_endpoint"]
    assert body["errors"][0]["type"] == "inference_endpoint_required"


@pytest.mark.integration
async def test_patch_model_rejects_clearing_the_endpoint_on_a_generic_row(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="urlless-patcher@example.com", roles=[admin_role])
    target = await create_model_service(
        db_session,
        get_settings(),
        name="patch-urlless",
        model_alias="patch-urlless",
        provider=ProviderVendor.GENERIC,
        provider_model_id="m",
        inference_endpoint="http://slm:8080/v1",
    )

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{target.id}",
        json={"inference_endpoint": None},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == status.HTTP_400_BAD_REQUEST
    assert body["title"] == "Bad Request"
    assert body["errors"][0]["loc"] == ["body", "inference_endpoint"]


@pytest.mark.integration
async def test_create_model_rejects_an_endpoint_without_a_url_scheme(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="schemeless-creator@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models",
        json=_create_payload(provider="generic", inference_endpoint="slm:8080/v1"),
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert ["body", "inference_endpoint"] in [item["loc"] for item in body["errors"]]


@pytest.mark.integration
async def test_patch_model_rejects_an_endpoint_without_a_url_scheme(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="schemeless-patcher@example.com", roles=[admin_role])
    target = await create_model_service(
        db_session,
        get_settings(),
        name="patch-schemeless",
        model_alias="patch-schemeless",
        provider=ProviderVendor.GENERIC,
        provider_model_id="m",
        inference_endpoint="http://slm:8080/v1",
    )

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{target.id}",
        json={"inference_endpoint": "slm:8080/v1"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    body = response.json()
    assert ["body", "inference_endpoint"] in [item["loc"] for item in body["errors"]]


@pytest.mark.integration
async def test_bulk_create_isolates_generic_row_without_endpoint(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    # A per-row 400 (not an envelope-level 422), so the sound rows still commit.
    # The bad row leads: it is rejected before touching the DB, so this is the
    # order where `apply_bulk`'s savepoint has had no autobegin to ride on.
    caller = await create_user_service(db_session, email="bulk-urlless@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models/bulk",
        json={
            "rows": [
                _bulk_row("bad-first", name="Broken first", model_alias="broken-first", provider="generic"),
                _bulk_row("ok", name="Fine", model_alias="fine"),
                _bulk_row("bad", name="Broken", model_alias="broken", provider="generic"),
            ]
        },
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    results = {row["row_key"]: row for row in response.json()["results"]}
    assert results["bad-first"]["error"]["status"] == status.HTTP_400_BAD_REQUEST
    assert results["ok"]["error"] is None
    assert results["bad"]["error"]["status"] == status.HTTP_400_BAD_REQUEST

    # The sound row still landed, so the leading rejection did not take the
    # outer transaction down with it.
    aliases = {model.model_alias for model in (await db_session.execute(AiModel.live_select())).scalars().all()}
    assert "fine" in aliases
    assert not {"broken-first", "broken"} & aliases


@pytest.mark.integration
async def test_create_model_does_not_leak_api_key(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="leak@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models",
        json=_create_payload(api_key="sk-very-secret"),
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["has_api_key"] is True
    assert "api_key" not in body
    assert "api_key_encrypted" not in body
    # And the raw secret never appears anywhere in the serialized body.
    assert "sk-very-secret" not in response.text


@pytest.mark.integration
async def test_list_models_returns_page(
    auth_db_client: AsyncClient, db_session: AsyncSession, reader_role: Role
) -> None:
    caller = await create_user_service(db_session, email="lister@example.com", roles=[reader_role])
    settings = get_settings()
    await create_model_service(
        db_session,
        settings,
        name="a",
        model_alias="a",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )
    await create_model_service(
        db_session,
        settings,
        name="b",
        model_alias="b",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )

    response = await auth_db_client.get(
        "/api/v1/ai-models",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 2
    assert {item["name"] for item in body["items"]} == {"a", "b"}


@pytest.mark.integration
async def test_list_models_excludes_disabled_for_reader(
    auth_db_client: AsyncClient, db_session: AsyncSession, reader_role: Role
) -> None:
    caller = await create_user_service(db_session, email="reader-disabled@example.com", roles=[reader_role])
    settings = get_settings()
    await create_model_service(
        db_session, settings, name="on", model_alias="on", provider=ProviderVendor.OPENAI, provider_model_id="gpt-4"
    )
    await create_model_service(
        db_session,
        settings,
        name="off",
        model_alias="off",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        is_disabled=True,
    )

    response = await auth_db_client.get(
        "/api/v1/ai-models",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 1
    assert {item["name"] for item in body["items"]} == {"on"}


@pytest.mark.integration
async def test_list_models_include_disabled_surfaces_disabled_for_admin(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="admin-disabled@example.com", roles=[admin_role])
    settings = get_settings()
    await create_model_service(
        db_session, settings, name="on", model_alias="on", provider=ProviderVendor.OPENAI, provider_model_id="gpt-4"
    )
    await create_model_service(
        db_session,
        settings,
        name="off",
        model_alias="off",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        is_disabled=True,
    )

    response = await auth_db_client.get(
        "/api/v1/ai-models?include_disabled=true",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 2
    assert {item["name"] for item in body["items"]} == {"on", "off"}


@pytest.mark.integration
async def test_list_models_include_disabled_forbidden_for_reader(
    auth_db_client: AsyncClient, db_session: AsyncSession, reader_role: Role
) -> None:
    """`models:read` alone may not opt into disabled rows — the flag rides on `models:update`."""
    caller = await create_user_service(db_session, email="reader-flag@example.com", roles=[reader_role])

    response = await auth_db_client.get(
        "/api/v1/ai-models?include_disabled=true",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.integration
async def test_get_disabled_model_returns_404_for_reader(
    auth_db_client: AsyncClient, db_session: AsyncSession, reader_role: Role
) -> None:
    caller = await create_user_service(db_session, email="reader-get-disabled@example.com", roles=[reader_role])
    target = await create_model_service(
        db_session,
        get_settings(),
        name="off-get",
        model_alias="off-get",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        is_disabled=True,
    )

    response = await auth_db_client.get(
        f"/api/v1/ai-models/{target.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.integration
async def test_get_disabled_model_visible_to_admin(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="admin-get-disabled@example.com", roles=[admin_role])
    target = await create_model_service(
        db_session,
        get_settings(),
        name="off-get-admin",
        model_alias="off-get-admin",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        is_disabled=True,
    )

    response = await auth_db_client.get(
        f"/api/v1/ai-models/{target.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["is_disabled"] is True


@pytest.mark.integration
async def test_get_model_returns_404_for_unknown(
    auth_db_client: AsyncClient, db_session: AsyncSession, reader_role: Role
) -> None:
    caller = await create_user_service(db_session, email="seek@example.com", roles=[reader_role])

    response = await auth_db_client.get(
        f"/api/v1/ai-models/{uuid4()}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.integration
async def test_patch_model_partial_update(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="patcher@example.com", roles=[admin_role])
    target = await create_model_service(
        db_session,
        get_settings(),
        name="orig",
        model_alias="orig",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{target.id}",
        json={"name": "renamed", "is_disabled": True},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["name"] == "renamed"
    assert body["is_disabled"] is True
    assert body["model_alias"] == "orig"


@pytest.mark.integration
async def test_patch_model_toggles_warmup_enabled(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="warmup-patcher@example.com", roles=[admin_role])
    target = await create_model_service(
        db_session,
        get_settings(),
        name="warmup-target",
        model_alias="warmup-target",
        provider=ProviderVendor.GENERIC,
        provider_model_id="qwen2.5-0.5b-instruct",
        inference_endpoint="http://slm:8080/v1",
    )
    assert target.warmup_enabled is False

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{target.id}",
        json={"warmup_enabled": True},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["warmup_enabled"] is True


@pytest.mark.integration
async def test_patch_model_toggles_advanced_params_disabled_without_dropping_params(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    """Flipping the flag suppresses the knobs but must not delete them — it has to be reversible."""
    caller = await create_user_service(db_session, email="adv-params-patcher@example.com", roles=[admin_role])
    target = await create_model_service(
        db_session,
        get_settings(),
        name="adv-params-target",
        model_alias="adv-params-target",
        provider=ProviderVendor.GENERIC,
        provider_model_id="qwen2.5-0.5b-instruct",
        inference_endpoint="http://slm:8080/v1",
        parameters={"temperature": 0.7},
    )
    assert target.advanced_params_disabled is False

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{target.id}",
        json={"advanced_params_disabled": True},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["advanced_params_disabled"] is True
    assert body["parameters"] == {"temperature": 0.7}


@pytest.mark.integration
async def test_patch_model_sets_and_clears_inactivity_alert_hours(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    """`null` clears the threshold (alerting off) rather than being rejected like a NOT NULL column."""
    caller = await create_user_service(db_session, email="inactivity-patcher@example.com", roles=[admin_role])
    target = await create_model_service(
        db_session,
        get_settings(),
        name="inactivity-target",
        model_alias="inactivity-target",
        provider=ProviderVendor.GENERIC,
        provider_model_id="qwen2.5-0.5b-instruct",
        inference_endpoint="http://slm:8080/v1",
    )
    headers = {"Authorization": f"Bearer {_token(caller)}"}

    set_response = await auth_db_client.patch(
        f"/api/v1/ai-models/{target.id}", json={"inactivity_alert_hours": 48}, headers=headers
    )
    clear_response = await auth_db_client.patch(
        f"/api/v1/ai-models/{target.id}", json={"inactivity_alert_hours": None}, headers=headers
    )

    assert set_response.status_code == status.HTTP_200_OK
    assert set_response.json()["inactivity_alert_hours"] == 48
    assert clear_response.status_code == status.HTTP_200_OK
    assert clear_response.json()["inactivity_alert_hours"] is None


@pytest.mark.integration
async def test_patch_model_rejects_non_positive_inactivity_alert_hours(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="inactivity-zero@example.com", roles=[admin_role])
    target = await create_model_service(
        db_session,
        get_settings(),
        name="inactivity-zero",
        model_alias="inactivity-zero",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{target.id}",
        json={"inactivity_alert_hours": 0},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.integration
async def test_patch_model_rejects_inactivity_alert_hours_above_a_year(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    # int4 column: an uncapped value would pass validation and surface as an undeclared 500.
    caller = await create_user_service(db_session, email="inactivity-cap@example.com", roles=[admin_role])
    target = await create_model_service(
        db_session,
        get_settings(),
        name="inactivity-cap",
        model_alias="inactivity-cap",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{target.id}",
        json={"inactivity_alert_hours": 8761},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.integration
async def test_create_model_persists_inactivity_alert_hours(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="inactivity-creator@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models",
        json={
            "name": "inactivity-new",
            "model_alias": "inactivity-new",
            "provider": ProviderVendor.OPENAI.value,
            "provider_model_id": "gpt-4",
            "warmup_enabled": True,
            "inactivity_alert_hours": 24,
        },
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["inactivity_alert_hours"] == 24
    assert body["last_used_at"] is None
    assert body["inactivity_alerted_at"] is None


@pytest.mark.integration
async def test_create_model_honours_advanced_params_disabled(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="adv-params-creator@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models",
        json=_create_payload(advanced_params_disabled=True, parameters={"temperature": 0.4}),
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["advanced_params_disabled"] is True
    assert body["parameters"] == {"temperature": 0.4}


@pytest.mark.integration
async def test_create_model_accepts_image_input_modality(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="vision-creator@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models",
        json=_create_payload(input_modalities=["text", "image"]),
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["input_modalities"] == ["text", "image"]


@pytest.mark.integration
async def test_create_model_rejects_input_without_text(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="no-text-in@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models",
        json=_create_payload(input_modalities=["image"]),
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.integration
async def test_create_model_rejects_empty_output_modalities(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="no-output@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models",
        json=_create_payload(output_modalities=[]),
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.integration
async def test_patch_model_rejects_empty_output_modalities(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    # `min_length=1` rides inside the `OutputModalities | None` union on the update
    # surface; a refactor or a pydantic bump could drop it there and leave create green.
    caller = await create_user_service(db_session, email="patch-no-output@example.com", roles=[admin_role])
    target = await create_model_service(
        db_session,
        get_settings(),
        name="Patch target",
        model_alias="patch-no-output",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
    )

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{target.id}",
        json={"output_modalities": []},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.integration
@pytest.mark.parametrize("retired", [{"modality": "text_to_image"}, {"supports_image_input": True}])
async def test_create_model_rejects_retired_modality_fields(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, retired: dict[str, object]
) -> None:
    # Ignoring these would apply the text/text default to a row whose author declared
    # image output — a chat completion sent to an image endpoint. The console's bulk
    # import forwards a pasted object verbatim, so last cycle's config reaches this.
    caller = await create_user_service(
        db_session, email=f"retired-{next(iter(retired))}@example.com", roles=[admin_role]
    )

    response = await auth_db_client.post(
        "/api/v1/ai-models",
        json=_create_payload() | retired,
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert "retired" in response.text


@pytest.mark.integration
async def test_create_model_canonicalises_modalities(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    # Duplicates and order carry no meaning, so a row must read back in enum order
    # with each modality once — otherwise equal declarations compare unequal.
    caller = await create_user_service(db_session, email="canon@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models",
        json=_create_payload(input_modalities=["image", "text", "image"], output_modalities=["image", "text"]),
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["input_modalities"] == ["text", "image"]
    assert body["output_modalities"] == ["text", "image"]


@pytest.mark.integration
async def test_patch_model_canonicalises_modalities(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    # The rule lives in the Annotated alias shared by create and update; without a
    # PATCH case, dropping it from the update surface alone would go unnoticed.
    caller = await create_user_service(db_session, email="patch-canon@example.com", roles=[admin_role])
    target = await create_model_service(
        db_session,
        get_settings(),
        name="patch-canon-target",
        model_alias="patch-canon-target",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
    )

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{target.id}",
        json={"input_modalities": ["image", "text", "image"], "output_modalities": ["image", "text"]},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["input_modalities"] == ["text", "image"]
    assert body["output_modalities"] == ["text", "image"]


@pytest.mark.integration
async def test_patch_projects_the_cleared_mismatch(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    # Branch coverage lives in the service tests; this pins that the response carries
    # the field at all, which the service test cannot see.
    caller = await create_user_service(db_session, email="mismatch-clear@example.com", roles=[admin_role])
    target = await create_model_service(
        db_session,
        get_settings(),
        name="mismatch-target",
        model_alias="mismatch-target",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        input_modalities=[Modality.TEXT, Modality.IMAGE],
    )
    target.capability_mismatch = "unsupported content type: image_url"
    await db_session.flush()

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{target.id}",
        json={"input_modalities": ["text"]},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["capability_mismatch"] is None


@pytest.mark.integration
async def test_a_stored_mismatch_reaches_the_wire(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    # The whole feature is one projected column; `default=None` on the response schema means an
    # `is None` assertion holds whether or not the projection exists. Both surfaces render it.
    caller = await create_user_service(db_session, email="mismatch-wire@example.com", roles=[admin_role])
    model = await create_model_service(
        db_session,
        get_settings(),
        name="mismatch-wire",
        model_alias="mismatch-wire",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        input_modalities=[Modality.TEXT, Modality.IMAGE],
    )
    model.capability_mismatch = "unsupported content type: image_url"
    await db_session.flush()
    headers = {"Authorization": f"Bearer {_token(caller)}"}

    detail = await auth_db_client.get(f"/api/v1/ai-models/{model.id}", headers=headers)
    listing = await auth_db_client.get("/api/v1/ai-models", headers=headers)

    assert detail.status_code == status.HTTP_200_OK
    assert detail.json()["capability_mismatch"] == "unsupported content type: image_url"
    assert listing.status_code == status.HTTP_200_OK
    rows = {row["id"]: row for row in listing.json()["items"]}
    assert rows[str(model.id)]["capability_mismatch"] == "unsupported content type: image_url"


@pytest.mark.integration
async def test_patch_model_rejects_input_without_text(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="patch-no-text@example.com", roles=[admin_role])
    target = await create_model_service(
        db_session,
        get_settings(),
        name="patch-no-text-target",
        model_alias="patch-no-text-target",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
    )

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{target.id}",
        json={"input_modalities": ["image"]},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.integration
async def test_patch_model_toggles_image_input_modality(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="vision-patcher@example.com", roles=[admin_role])
    target = await create_model_service(
        db_session,
        get_settings(),
        name="vision-target",
        model_alias="vision-target",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
    )
    assert target.input_modalities == [Modality.TEXT]

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{target.id}",
        json={"input_modalities": ["text", "image"]},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["input_modalities"] == ["text", "image"]


@pytest.mark.integration
async def test_patch_parameters_one_knob_lands_no_null_keys(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    """PATCHing `parameters` must not persist `null` knobs in the JSONB.

    Two-state cascade: a knob present overrides, absent inherits — there is no
    "clear" state. `exclude_unset` recurses into the nested model, so an
    explicitly-null knob would otherwise survive; the route strips it
    (`exclude_none`) to match POST. Sending one real knob alongside an
    explicit `null` guards that end-to-end through route serialization.
    """
    caller = await create_user_service(db_session, email="knob@example.com", roles=[admin_role])
    target = await create_model_service(
        db_session,
        get_settings(),
        name="knob",
        model_alias="knob",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{target.id}",
        json={"parameters": {"temperature": 0.5, "top_p": None}},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["parameters"] == {"temperature": 0.5}


@pytest.mark.integration
async def test_put_api_key_sets_has_api_key(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="rotator@example.com", roles=[admin_role])
    target = await create_model_service(
        db_session,
        get_settings(),
        name="keyless",
        model_alias="keyless",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )

    put = await auth_db_client.put(
        f"/api/v1/ai-models/{target.id}/api-key",
        json={"api_key": "sk-new"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )
    assert put.status_code == status.HTTP_204_NO_CONTENT

    get = await auth_db_client.get(
        f"/api/v1/ai-models/{target.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )
    assert get.status_code == status.HTTP_200_OK
    assert get.json()["has_api_key"] is True


@pytest.mark.integration
async def test_delete_api_key_clears_credential(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="clearer@example.com", roles=[admin_role])
    target = await create_model_service(
        db_session,
        get_settings(),
        name="keyed",
        model_alias="keyed",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        api_key=SecretStr("sk-existing"),
    )

    delete = await auth_db_client.delete(
        f"/api/v1/ai-models/{target.id}/api-key",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )
    assert delete.status_code == status.HTTP_204_NO_CONTENT

    get = await auth_db_client.get(
        f"/api/v1/ai-models/{target.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )
    assert get.json()["has_api_key"] is False


@pytest.mark.integration
async def test_delete_model_soft_deletes(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="deleter@example.com", roles=[admin_role])
    target = await create_model_service(
        db_session,
        get_settings(),
        name="goodbye",
        model_alias="goodbye",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )

    delete = await auth_db_client.delete(
        f"/api/v1/ai-models/{target.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )
    assert delete.status_code == status.HTTP_204_NO_CONTENT

    follow_up = await auth_db_client.get(
        f"/api/v1/ai-models/{target.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )
    assert follow_up.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_delete_model_removes_it_from_group_subsets(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="deleter-subset@example.com", roles=[admin_role])
    group = await persist_evaluation_group(db_session)
    model = await _persist_model(db_session, "subset-goodbye")
    db_session.add(EvaluationGroupAiModel(evaluation_group_id=group.id, model_id=model.id))
    await db_session.flush()
    group_id = group.id

    delete = await auth_db_client.delete(
        f"/api/v1/ai-models/{model.id}", headers={"Authorization": f"Bearer {_token(caller)}"}
    )
    assert delete.status_code == status.HTTP_204_NO_CONTENT

    live_subset = (
        (
            await db_session.execute(
                EvaluationGroupAiModel.live_select().where(col(EvaluationGroupAiModel.evaluation_group_id) == group_id)
            )
        )
        .scalars()
        .all()
    )
    assert live_subset == []


@pytest.mark.integration
@pytest.mark.parametrize(
    "field",
    [
        "name",
        "model_alias",
        "provider",
        "input_modalities",
        "output_modalities",
        "provider_model_id",
        "parameters",
        "extras",
        "is_disabled",
        "warmup_enabled",
        "advanced_params_disabled",
    ],
)
async def test_patch_model_rejects_explicit_null_on_required_columns(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    admin_role: Role,
    field: str,
) -> None:
    """Explicit `null` on a NOT NULL column must 422, not slip through to a misleading 409."""
    caller = await create_user_service(db_session, email=f"null-{field}@example.com", roles=[admin_role])
    target = await create_model_service(
        db_session,
        get_settings(),
        name=f"null-{field}",
        model_alias=f"null-{field}",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{target.id}",
        json={field: None},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert any(field in err["loc"] for err in body["errors"])


# ---------------------------------------------------------------------------
# POST /api/v1/ai-models/bulk
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_bulk_create_models_commits_all_rows(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="bulk-ok@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models/bulk",
        json={
            "rows": [
                _bulk_row("r1", name="Model One", model_alias="model-one"),
                _bulk_row("r2", name="Model Two", model_alias="model-two", api_key="sk-secret-two"),
            ],
        },
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["dry_run"] is False
    assert (body["total"], body["succeeded"], body["failed"]) == (2, 2, 0)

    by_key = {row["row_key"]: row for row in body["results"]}
    assert by_key["r1"]["status"] == "ok"
    assert by_key["r1"]["data"]["has_api_key"] is False
    assert by_key["r2"]["status"] == "ok"
    assert by_key["r2"]["data"]["has_api_key"] is True
    # The inline plaintext key is encrypted at rest and never echoed back.
    assert "sk-secret-two" not in response.text

    persisted = (await db_session.execute(AiModel.live_select())).scalars().all()
    assert {model.model_alias for model in persisted} == {"model-one", "model-two"}


@pytest.mark.integration
async def test_bulk_create_models_carries_labels_through(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="bulk-labels@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models/bulk",
        json={
            "rows": [
                _bulk_row("r1", name="Imported", model_alias="imported", labels=["  self-hosted ", "SELF-HOSTED"]),
            ],
        },
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    # The console's bulk import forwards a pasted object verbatim, so the imported row must get the
    # same canonicalisation the single create does — not the raw strings.
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert (body["total"], body["succeeded"], body["failed"]) == (1, 1, 0)
    assert body["results"][0]["data"]["labels"] == ["self-hosted"]


@pytest.mark.integration
async def test_bulk_create_models_per_row_conflict_is_isolated(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="bulk-conflict@example.com", roles=[admin_role])
    # Different provider than the bulk row's default (`anthropic`) — name alone collides.
    await create_model_service(
        db_session,
        get_settings(),
        name="taken",
        model_alias="taken-alias",
        provider=ProviderVendor.OPENAI,
        provider_model_id="claude-3",
    )

    response = await auth_db_client.post(
        "/api/v1/ai-models/bulk",
        json={
            "rows": [
                _bulk_row("ok", name="Fresh", model_alias="fresh-alias"),
                # name collides with the pre-existing live row — fails at the DB.
                _bulk_row("conflict", name="taken", model_alias="other-alias"),
            ],
        },
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert (body["succeeded"], body["failed"]) == (1, 1)

    by_key = {row["row_key"]: row for row in body["results"]}
    assert by_key["ok"]["status"] == "ok"
    assert by_key["conflict"]["status"] == "failed"
    assert by_key["conflict"]["error"]["status"] == status.HTTP_409_CONFLICT

    # The successful row committed; the conflicting row's savepoint rolled back,
    # so its alias never landed.
    aliases = {model.model_alias for model in (await db_session.execute(AiModel.live_select())).scalars().all()}
    assert "fresh-alias" in aliases
    assert "other-alias" not in aliases


@pytest.mark.integration
async def test_bulk_create_models_dry_run_previews_and_rolls_back(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    # Dry-run rolls the whole session transaction back, so post-request
    # assertions are emptiness checks — fixture rows are gone too.
    caller = await create_user_service(db_session, email="bulk-dry@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models/bulk",
        json={
            "rows": [_bulk_row("preview", name="Preview", model_alias="preview-alias")],
            "dry_run": True,
        },
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["dry_run"] is True
    assert (body["succeeded"], body["failed"]) == (1, 0)
    assert body["results"][0]["data"]["model_alias"] == "preview-alias"

    persisted = (await db_session.execute(AiModel.live_select())).scalars().all()
    assert persisted == []


@pytest.mark.integration
@pytest.mark.parametrize(
    ("first", "second", "expected_msg_fragment"),
    [
        # Same name, distinct aliases — trips the duplicate-name guard.
        ({"name": "Dup", "model_alias": "alias-a"}, {"name": "Dup", "model_alias": "alias-b"}, "Duplicate name: 'Dup'"),
        # Same alias, distinct names — trips the duplicate-alias guard.
        (
            {"name": "Name A", "model_alias": "dup-alias"},
            {"name": "Name B", "model_alias": "dup-alias"},
            "Duplicate model_alias: 'dup-alias'",
        ),
        # Same name, distinct providers — name is unique regardless of provider.
        (
            {"name": "Llama 3", "model_alias": "llama-3-hf", "provider": "huggingface"},
            {"name": "Llama 3", "model_alias": "llama-3-bedrock", "provider": "aws_bedrock"},
            "Duplicate name: 'Llama 3'",
        ),
    ],
)
async def test_bulk_create_models_duplicate_identity_in_batch_returns_422(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    admin_role: Role,
    first: dict[str, object],
    second: dict[str, object],
    expected_msg_fragment: str,
) -> None:
    caller = await create_user_service(db_session, email="bulk-dup@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models/bulk",
        json={"rows": [_bulk_row("r1", **first), _bulk_row("r2", **second)]},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert expected_msg_fragment in body["errors"][0]["msg"]
    # Envelope-level rejection — the batch never reaches the processor.
    persisted = (await db_session.execute(AiModel.live_select())).scalars().all()
    assert persisted == []


# ---------------------------------------------------------------------------
# POST /api/v1/ai-models/api-keys/bulk
# ---------------------------------------------------------------------------


async def _seed_model(db_session: AsyncSession, *, name: str, provider: ProviderVendor, alias: str) -> AiModel:
    return await create_model_service(
        db_session,
        get_settings(),
        name=name,
        model_alias=alias,
        provider=provider,
        provider_model_id="m",
    )


@pytest.mark.integration
async def test_bulk_set_api_keys_sets_keys(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="bulk-key-ok@example.com", roles=[admin_role])
    await _seed_model(db_session, name="Alpha", provider=ProviderVendor.OPENAI, alias="alpha")
    await _seed_model(db_session, name="Beta", provider=ProviderVendor.ANTHROPIC, alias="beta")

    response = await auth_db_client.post(
        "/api/v1/ai-models/api-keys/bulk",
        json={
            "rows": [
                _key_row("r1", name="Alpha", api_key="sk-alpha"),
                _key_row("r2", name="Beta", api_key="sk-beta"),
            ],
        },
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["dry_run"] is False
    assert (body["total"], body["succeeded"], body["failed"]) == (2, 2, 0)
    assert all(row["data"]["has_api_key"] is True for row in body["results"])
    # The plaintext keys are encrypted at rest and never echoed back.
    assert "sk-alpha" not in response.text
    assert "sk-beta" not in response.text

    persisted = (await db_session.execute(AiModel.live_select())).scalars().all()
    stored = {model.name: model.api_key_encrypted for model in persisted}
    assert stored.keys() == {"Alpha", "Beta"}
    # A decrypt round-trip proves the ciphertext is the real key, not just non-null.
    assert stored["Alpha"] is not None
    assert stored["Beta"] is not None
    assert decrypt_secret(stored["Alpha"], get_settings()) == "sk-alpha"
    assert decrypt_secret(stored["Beta"], get_settings()) == "sk-beta"


@pytest.mark.integration
async def test_bulk_set_api_keys_missing_target_is_isolated(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="bulk-key-missing@example.com", roles=[admin_role])
    await _seed_model(db_session, name="Alpha", provider=ProviderVendor.OPENAI, alias="alpha")

    response = await auth_db_client.post(
        "/api/v1/ai-models/api-keys/bulk",
        json={
            "rows": [
                _key_row("ok", name="Alpha", api_key="sk-alpha"),
                # No live model with this name — resolves to a per-row 404.
                _key_row("missing", name="Ghost", api_key="sk-ghost"),
            ],
        },
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert (body["succeeded"], body["failed"]) == (1, 1)
    by_key = {row["row_key"]: row for row in body["results"]}
    assert by_key["ok"]["status"] == "ok"
    assert by_key["missing"]["status"] == "failed"
    assert by_key["missing"]["error"]["status"] == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_bulk_set_api_keys_duplicate_target_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="bulk-key-dup@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models/api-keys/bulk",
        json={
            "rows": [
                _key_row("r1", name="Alpha", api_key="sk-1"),
                _key_row("r2", name="Alpha", api_key="sk-2"),
            ],
        },
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert "Duplicate target: name 'Alpha'" in body["errors"][0]["msg"]


@pytest.mark.integration
async def test_bulk_set_api_keys_dry_run_does_not_persist(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    # Commit the seed first so it survives the dry-run's session rollback: the
    # `create_savepoint` test mode turns this commit into a released savepoint
    # the endpoint's rollback can't undo. Otherwise the seed shares the
    # rolled-back transaction and we could only prove emptiness — not that a
    # *pre-existing committed* key is left untouched, which is the real
    # rotation guarantee of a dry-run.
    caller = await create_user_service(db_session, email="bulk-key-dry@example.com", roles=[admin_role])
    model = await _seed_model(db_session, name="Alpha", provider=ProviderVendor.OPENAI, alias="alpha")
    await db_session.commit()

    response = await auth_db_client.post(
        "/api/v1/ai-models/api-keys/bulk",
        json={
            "rows": [_key_row("preview", name="Alpha", api_key="sk-alpha")],
            "dry_run": True,
        },
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["dry_run"] is True
    assert (body["succeeded"], body["failed"]) == (1, 0)
    assert body["results"][0]["data"]["has_api_key"] is True

    # The committed row still exists and its key was never written — the preview
    # rolled the encrypted-key update back.
    await db_session.refresh(model)
    assert model.api_key_encrypted is None


async def _persist_model(db_session: AsyncSession, alias: str) -> AiModel:
    model = AiModel(name=alias, model_alias=alias, provider=ProviderVendor.ANTHROPIC, provider_model_id=f"{alias}-id")
    db_session.add(model)
    await db_session.flush()
    return model


@pytest.mark.integration
async def test_list_assignable_to_evaluation_filters_subset_minus_assigned(
    auth_db_client: AsyncClient, db_session: AsyncSession, reader_role: Role
) -> None:
    caller = await create_user_service(db_session, email="assignable@example.com", roles=[reader_role])
    group = await persist_evaluation_group(db_session)
    evaluation = Evaluation(title="E", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(evaluation)
    await db_session.flush()
    free = await _persist_model(db_session, "m-free")
    taken = await _persist_model(db_session, "m-taken")
    await _persist_model(db_session, "m-outside")  # exists but not in the subset — must be filtered out
    for model in (free, taken):
        db_session.add(EvaluationGroupAiModel(evaluation_group_id=group.id, model_id=model.id))
    db_session.add(EvaluationAiModel(evaluation_id=evaluation.id, model_id=taken.id))
    await db_session.flush()

    response = await auth_db_client.get(
        f"/api/v1/ai-models?assignable_to_evaluation={evaluation.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    ids = {item["id"] for item in response.json()["items"]}
    # `free` is in the subset and unassigned; `taken` is assigned; `outsider` is not in the subset.
    assert ids == {str(free.id)}


@pytest.mark.integration
async def test_list_assignable_to_evaluation_empty_subset_returns_none(
    auth_db_client: AsyncClient, db_session: AsyncSession, reader_role: Role
) -> None:
    caller = await create_user_service(db_session, email="assignable-empty@example.com", roles=[reader_role])
    group = await persist_evaluation_group(db_session)
    evaluation = Evaluation(title="E", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(evaluation)
    await db_session.flush()
    await _persist_model(db_session, "exists-but-not-allowed")

    response = await auth_db_client.get(
        f"/api/v1/ai-models?assignable_to_evaluation={evaluation.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["total"] == 0


@pytest.mark.integration
async def test_list_assignable_to_unknown_evaluation_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, reader_role: Role
) -> None:
    caller = await create_user_service(db_session, email="assignable-404@example.com", roles=[reader_role])

    response = await auth_db_client.get(
        f"/api/v1/ai-models?assignable_to_evaluation={uuid4()}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_health_check_starts_and_enqueues(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueued: list[dict[str, object]] = []
    monkeypatch.setattr(ai_models_route.run_model_health_check, "apply_async", lambda *a, **k: enqueued.append(k))
    caller = await create_user_service(db_session, email="hc-admin@example.com", roles=[admin_role])
    model = await create_model_service(
        db_session,
        get_settings(),
        name="hc",
        model_alias="hc",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )
    response = await auth_db_client.post(
        f"/api/v1/ai-models/{model.id}/health-check", headers={"Authorization": f"Bearer {_token(caller)}"}
    )
    assert response.status_code == status.HTTP_202_ACCEPTED
    body = response.json()
    # All four health fields serialize on the response (checking; start stamped; not yet dead/alive).
    assert body["health_check_status"] == "checking"
    assert body["last_health_check_at"] is not None
    assert body["last_health_reason"] is None
    assert body["last_healthy_at"] is None
    assert enqueued == [{"args": [str(model.id)], "countdown": 1}]


@pytest.mark.integration
async def test_health_check_idempotent_while_checking(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(ai_models_route.run_model_health_check, "apply_async", lambda *a, **k: calls.append(a))
    caller = await create_user_service(db_session, email="hc-admin2@example.com", roles=[admin_role])
    model = await create_model_service(
        db_session,
        get_settings(),
        name="hc2",
        model_alias="hc2",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )
    hdr = {"Authorization": f"Bearer {_token(caller)}"}
    first = await auth_db_client.post(f"/api/v1/ai-models/{model.id}/health-check", headers=hdr)
    second = await auth_db_client.post(f"/api/v1/ai-models/{model.id}/health-check", headers=hdr)
    assert first.status_code == status.HTTP_202_ACCEPTED
    assert second.status_code == status.HTTP_200_OK  # already checking, fresh → no second task
    assert second.json()["health_check_status"] == "checking"  # idempotent response carries the in-flight row
    assert len(calls) == 1


@pytest.mark.integration
async def test_health_check_forbidden_without_models_update(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, reader_role: Role
) -> None:
    reader = await create_user_service(db_session, email="hc-reader@example.com", roles=[reader_role])
    model = await create_model_service(
        db_session,
        get_settings(),
        name="hc3",
        model_alias="hc3",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )
    response = await auth_db_client.post(
        f"/api/v1/ai-models/{model.id}/health-check", headers={"Authorization": f"Bearer {_token(reader)}"}
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_health_check_unknown_model_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="hc-404@example.com", roles=[admin_role])
    response = await auth_db_client.post(
        "/api/v1/ai-models/11111111-1111-1111-1111-111111111111/health-check",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_list_assignable_to_evaluation_hidden_group_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, reader_role: Role
) -> None:
    # An invitation-only group is invisible to a non-member, so its subset must not
    # leak through the picker — the evaluation reads as missing (404), not 200.
    caller = await create_user_service(db_session, email="assignable-hidden@example.com", roles=[reader_role])
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    evaluation = Evaluation(title="E", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(evaluation)
    await db_session.flush()
    model = await _persist_model(db_session, "hidden-allowed")
    db_session.add(EvaluationGroupAiModel(evaluation_group_id=group.id, model_id=model.id))
    await db_session.flush()

    response = await auth_db_client.get(
        f"/api/v1/ai-models?assignable_to_evaluation={evaluation.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_list_assignable_to_evaluation_visible_to_group_member(
    auth_db_client: AsyncClient, db_session: AsyncSession, reader_role: Role
) -> None:
    # The group owner is a member, so an invitation-only group is visible and the picker works.
    caller = await create_user_service(db_session, email="assignable-member@example.com", roles=[reader_role])
    group = await persist_evaluation_group(
        db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY, created_by_id=caller.id
    )
    evaluation = Evaluation(title="E", description="d", evaluation_group_id=group.id, created_by_id=caller.id)
    db_session.add(evaluation)
    await db_session.flush()
    model = await _persist_model(db_session, "member-allowed")
    db_session.add(EvaluationGroupAiModel(evaluation_group_id=group.id, model_id=model.id))
    await db_session.flush()

    response = await auth_db_client.get(
        f"/api/v1/ai-models?assignable_to_evaluation={evaluation.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert {item["id"] for item in response.json()["items"]} == {str(model.id)}


async def _persist_plain_user(db_session: AsyncSession, email: str) -> User:
    """A user whose global role grants no `models:read` (object roles come separately)."""
    role = Role(
        name=f"plain-{email}", description="No model access", permissions=[Permission.EVALUATION_GROUPS_READ.value]
    )
    db_session.add(role)
    await db_session.flush()
    return await create_user_service(db_session, email=email, roles=[role])


@pytest.mark.integration
async def test_list_for_group_authorizes_in_group_owner_without_global_read(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    # An in-group owner lacking the global `models:read` may list the whole registry
    # (not just the group's subset) via `for_group`, to curate the subset.
    caller = await _persist_plain_user(db_session, "for-group-owner@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)
    a = await _persist_model(db_session, "m-a")
    b = await _persist_model(db_session, "m-b")  # not linked to the group — must still appear

    response = await auth_db_client.get(
        f"/api/v1/ai-models?for_group={group.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert {str(a.id), str(b.id)} <= {item["id"] for item in response.json()["items"]}


@pytest.mark.integration
async def test_list_for_group_hidden_group_returns_404(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await _persist_plain_user(db_session, "for-group-outsider@example.com")
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)

    response = await auth_db_client.get(
        f"/api/v1/ai-models?for_group={group.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_list_for_group_visible_group_without_permission_returns_403(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    # A public group is visible to the non-member caller, but they hold no in-group
    # role granting `models:read` → 403 (not 404).
    caller = await _persist_plain_user(db_session, "for-group-visible@example.com")
    group = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.PUBLIC)

    response = await auth_db_client.get(
        f"/api/v1/ai-models?for_group={group.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_list_without_global_read_or_for_group_returns_403(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    caller = await _persist_plain_user(db_session, "no-read@example.com")

    response = await auth_db_client.get(
        "/api/v1/ai-models",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_list_for_group_owner_cannot_include_disabled(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    # `for_group` authorizes reading enabled models; surfacing disabled ones still
    # needs the global `models:update`, which an in-group owner does not hold.
    caller = await _persist_plain_user(db_session, "for-group-disabled@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)

    response = await auth_db_client.get(
        f"/api/v1/ai-models?for_group={group.id}&include_disabled=true",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_list_assignable_to_evaluation_authorizes_in_group_owner_without_global_read(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    # An in-group owner without the global `models:read` may list an evaluation's
    # assignable models (to pick one to assign), authorized via the parent group.
    caller = await _persist_plain_user(db_session, "assignable-owner@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)
    evaluation = Evaluation(title="E", description="d", evaluation_group_id=group.id, created_by_id=caller.id)
    db_session.add(evaluation)
    await db_session.flush()
    allowed = await _persist_model(db_session, "assignable-a")
    db_session.add(EvaluationGroupAiModel(evaluation_group_id=group.id, model_id=allowed.id))
    await db_session.flush()

    response = await auth_db_client.get(
        f"/api/v1/ai-models?assignable_to_evaluation={evaluation.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert {item["id"] for item in response.json()["items"]} == {str(allowed.id)}


@pytest.mark.integration
async def test_list_for_group_cannot_authorize_another_groups_assignable_subset(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    # `assignable_to_evaluation` scopes the result to its evaluation's parent group, so
    # authorization must target that group — a caller may not substitute a group they own
    # (`for_group`) to read a group they only *see* but hold no `models:read` on.
    caller = await _persist_plain_user(db_session, "cross-group@example.com")
    owned = await persist_evaluation_group(db_session, created_by_id=caller.id)  # caller owns it → models:read
    other = await persist_evaluation_group(db_session, access_level=EvaluationGroupAccessLevel.PUBLIC)
    evaluation = Evaluation(title="E", description="d", evaluation_group_id=other.id, created_by_id=other.created_by_id)
    db_session.add(evaluation)
    await db_session.flush()
    secret = await _persist_model(db_session, "other-subset")
    db_session.add(EvaluationGroupAiModel(evaluation_group_id=other.id, model_id=secret.id))
    await db_session.flush()

    response = await auth_db_client.get(
        f"/api/v1/ai-models?for_group={owned.id}&assignable_to_evaluation={evaluation.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_health_check_disabled_model_is_checkable(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Design decision: a disabled model IS checkable (verify an endpoint before enabling it).
    enqueued: list[dict[str, object]] = []
    monkeypatch.setattr(ai_models_route.run_model_health_check, "apply_async", lambda *a, **k: enqueued.append(k))
    caller = await create_user_service(db_session, email="hc-disabled@example.com", roles=[admin_role])
    model = await create_model_service(
        db_session,
        get_settings(),
        name="hcd",
        model_alias="hcd",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        is_disabled=True,
    )
    response = await auth_db_client.post(
        f"/api/v1/ai-models/{model.id}/health-check", headers={"Authorization": f"Bearer {_token(caller)}"}
    )
    assert response.status_code == status.HTTP_202_ACCEPTED
    assert response.json()["health_check_status"] == "checking"
    assert enqueued == [{"args": [str(model.id)], "countdown": 1}]


@pytest.mark.integration
async def test_health_check_stale_checking_starts_a_new_check(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A `checking` row older than the stale TTL is treated as abandoned (crashed worker); a fresh POST
    # overrides it and enqueues a new task — the feature's only self-heal (no reaper).
    enqueued: list[dict[str, object]] = []
    monkeypatch.setattr(ai_models_route.run_model_health_check, "apply_async", lambda *a, **k: enqueued.append(k))
    caller = await create_user_service(db_session, email="hc-stale@example.com", roles=[admin_role])
    model = await create_model_service(
        db_session,
        get_settings(),
        name="hcs",
        model_alias="hcs",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )
    model.health_check_status = HealthCheckStatus.CHECKING
    # An hour old — far past the abandonment horizon (the task's time_limit) regardless of config.
    model.last_health_check_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.flush()

    response = await auth_db_client.post(
        f"/api/v1/ai-models/{model.id}/health-check", headers={"Authorization": f"Bearer {_token(caller)}"}
    )
    assert response.status_code == status.HTTP_202_ACCEPTED  # stale checking → override, not idempotent 200
    assert enqueued == [{"args": [str(model.id)], "countdown": 1}]


async def _delete_model(client: AsyncClient, token: str, model_id: UUID) -> Response:
    return await client.delete(f"/api/v1/ai-models/{model_id}", headers={"Authorization": f"Bearer {token}"})


async def _restore_model(client: AsyncClient, token: str, model_id: UUID) -> Response:
    return await client.post(f"/api/v1/ai-models/{model_id}/restore", headers={"Authorization": f"Bearer {token}"})


async def _backdate_model_tombstone(db_session: AsyncSession, model_id: UUID, *, days: int) -> None:
    """Age a tombstone past the restore window. Plain `select` — it must see deleted rows."""
    db_session.expire_all()
    row = (await db_session.execute(select(AiModel).where(col(AiModel.id) == model_id))).scalar_one()
    row.deleted_at = datetime.now(UTC) - timedelta(days=days)
    db_session.add(row)
    await db_session.flush()


@pytest.mark.integration
async def test_restore_model_returns_it_to_the_registry(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="restore-model@example.com", roles=[admin_role])
    token = _token(caller)
    model = await _persist_model(db_session, "restore-me")
    model_id = model.id
    assert (await _delete_model(auth_db_client, token, model_id)).status_code == status.HTTP_204_NO_CONTENT

    response = await _restore_model(auth_db_client, token, model_id)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted_at"] is None
    follow_up = await auth_db_client.get(f"/api/v1/ai-models/{model_id}", headers={"Authorization": f"Bearer {token}"})
    assert follow_up.status_code == status.HTTP_200_OK


@pytest.mark.integration
async def test_restore_model_does_not_revive_its_assignments(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    # Restore is shallow by design: the model returns to the registry, but the
    # assignments its delete removed stay gone — re-assigning is the forward action.
    caller = await create_user_service(db_session, email="restore-shallow@example.com", roles=[admin_role])
    token = _token(caller)
    group = await persist_evaluation_group(db_session)
    evaluation = Evaluation(title="E", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(evaluation)
    await db_session.flush()
    model = await _persist_model(db_session, "shallow-restore")
    assignment = EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id)
    db_session.add(assignment)
    await db_session.flush()
    model_id, assignment_id = model.id, assignment.id
    await _delete_model(auth_db_client, token, model_id)

    assert (await _restore_model(auth_db_client, token, model_id)).status_code == status.HTTP_200_OK

    db_session.expire_all()
    revived = (
        await db_session.execute(select(EvaluationAiModel).where(col(EvaluationAiModel.id) == assignment_id))
    ).scalar_one()
    assert revived.deleted_at is not None


@pytest.mark.integration
async def test_restore_model_409s_when_a_live_row_took_its_name(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    # `name` and `model_alias` are unique among *live* rows only, so the delete freed
    # both — a re-registered model holds them now and the tombstone can't come back.
    caller = await create_user_service(db_session, email="restore-clash@example.com", roles=[admin_role])
    token = _token(caller)
    model = await _persist_model(db_session, "taken-name")
    model_id = model.id
    await _delete_model(auth_db_client, token, model_id)
    replacement = await auth_db_client.post(
        "/api/v1/ai-models",
        json=_create_payload(name="taken-name", model_alias="taken-name-2"),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert replacement.status_code == status.HTTP_201_CREATED

    response = await _restore_model(auth_db_client, token, model_id)

    assert response.status_code == status.HTTP_409_CONFLICT
    assert "name" in response.json()["detail"]


@pytest.mark.integration
async def test_restore_model_404s_outside_the_window(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="restore-stale@example.com", roles=[admin_role])
    token = _token(caller)
    model = await _persist_model(db_session, "too-old")
    model_id = model.id
    await _delete_model(auth_db_client, token, model_id)
    await _backdate_model_tombstone(db_session, model_id, days=30)

    response = await _restore_model(auth_db_client, token, model_id)

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_restore_model_requires_models_delete(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, reader_role: Role
) -> None:
    admin = await create_user_service(db_session, email="restore-admin@example.com", roles=[admin_role])
    reader = await create_user_service(db_session, email="restore-reader@example.com", roles=[reader_role])
    model = await _persist_model(db_session, "perm-gated")
    model_id = model.id
    await _delete_model(auth_db_client, _token(admin), model_id)

    response = await _restore_model(auth_db_client, _token(reader), model_id)

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_deleted_listing_serves_tombstones_newest_first(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    # This list has no `order_by`, so the deleted view fixes its own order — creation
    # order would bury the delete you just made.
    caller = await create_user_service(db_session, email="deleted-list@example.com", roles=[admin_role])
    token = _token(caller)
    first = await _persist_model(db_session, "gone-first")
    second = await _persist_model(db_session, "gone-second")
    kept = await _persist_model(db_session, "still-here")
    first_id, second_id, kept_id = first.id, second.id, kept.id
    await _delete_model(auth_db_client, token, first_id)
    await _delete_model(auth_db_client, token, second_id)

    response = await auth_db_client.get("/api/v1/ai-models?deleted=true", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == status.HTTP_200_OK
    items = response.json()["items"]
    listed = [item["id"] for item in items]
    assert listed == [str(second_id), str(first_id)]
    assert str(kept_id) not in listed
    # Every projection in this family would pass with `deleted_by_id` dropped from its
    # `from_model` kwargs; this is the one assertion that catches that.
    assert {item["deleted_by_id"] for item in items} == {str(caller.id)}
    assert all(item["deleted_at"] is not None for item in items)


@pytest.mark.integration
async def test_deleted_listing_requires_models_delete(
    auth_db_client: AsyncClient, db_session: AsyncSession, reader_role: Role
) -> None:
    # Privileged flag, rejected rather than silently dropped — the same contract
    # `include_disabled` already has on this endpoint.
    reader = await create_user_service(db_session, email="deleted-list-reader@example.com", roles=[reader_role])

    response = await auth_db_client.get(
        "/api/v1/ai-models?deleted=true", headers={"Authorization": f"Bearer {_token(reader)}"}
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_deleted_listing_includes_a_disabled_tombstone(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    # A row disabled *then* deleted is still restorable, so the deleted view must not
    # inherit the live list's `disabled_at IS NULL` default.
    caller = await create_user_service(db_session, email="deleted-disabled@example.com", roles=[admin_role])
    token = _token(caller)
    model = await _persist_model(db_session, "disabled-then-deleted")
    model.disabled_at = datetime.now(UTC)
    await db_session.flush()
    model_id = model.id
    await _delete_model(auth_db_client, token, model_id)

    response = await auth_db_client.get("/api/v1/ai-models?deleted=true", headers={"Authorization": f"Bearer {token}"})

    assert [item["id"] for item in response.json()["items"]] == [str(model_id)]


@pytest.mark.integration
async def test_create_model_stores_canonicalised_labels(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="labeller@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models",
        json=_create_payload(labels=["  self-hosted ", "Fine-tuning needed", "SELF-HOSTED"]),
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["labels"] == ["Fine-tuning needed", "self-hosted"]


@pytest.mark.integration
async def test_patch_model_replaces_labels_wholesale(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="relabeller@example.com", roles=[admin_role])
    model = await create_model_service(
        db_session,
        get_settings(),
        name="Relabel me",
        model_alias="relabel-me",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        labels=["stale", "self-hosted"],
    )

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{model.id}",
        json={"labels": ["audited"]},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["labels"] == ["audited"]


@pytest.mark.integration
async def test_patch_model_rejects_explicit_null_labels(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="nuller@example.com", roles=[admin_role])
    model = await create_model_service(
        db_session,
        get_settings(),
        name="Keep my labels",
        model_alias="keep-my-labels",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        labels=["audited"],
    )

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{model.id}",
        json={"labels": None},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.headers["content-type"].startswith("application/problem+json")
    assert any("labels" in error["loc"] for error in response.json()["errors"])


@pytest.mark.integration
async def test_list_labels_returns_a_flat_array(
    auth_db_client: AsyncClient, db_session: AsyncSession, reader_role: Role
) -> None:
    caller = await create_user_service(db_session, email="vocab@example.com", roles=[reader_role])
    await create_model_service(
        db_session,
        get_settings(),
        name="Labelled one",
        model_alias="labelled-one",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        labels=["self-hosted"],
    )

    # Also the routing guard: `/labels` is declared before `/{model_id}`, so a 422 here would mean
    # the UUID converter claimed the path. Which rows contribute is the service suite's branch.
    response = await auth_db_client.get(
        "/api/v1/ai-models/labels",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == ["self-hosted"]


@pytest.mark.integration
async def test_list_labels_requires_models_read(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await caller_with(db_session)

    response = await auth_db_client.get(
        "/api/v1/ai-models/labels",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_create_declaring_image_starts_a_check_without_being_asked(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The capability probe only runs inside a health check, and that is manual — so a declaration
    # nobody checks is the default. Firing it here is what closes the gap for the operator.
    enqueued: list[dict[str, object]] = []
    monkeypatch.setattr(ai_models_route.run_model_health_check, "apply_async", lambda *a, **k: enqueued.append(k))
    caller = await create_user_service(db_session, email="autocheck-create@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models",
        json=_create_payload(input_modalities=["text", "image"], api_key="sk-probe"),
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    # The write is not gated on the probe: the row is created and the answer follows.
    assert response.json()["health_check_status"] == "checking"
    assert enqueued == [{"args": [response.json()["id"]], "countdown": 1}]


@pytest.mark.integration
async def test_create_with_nothing_to_call_starts_no_check(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The console offers "Set API key" as a separate action, so create-then-key is supported. A
    # check here could only settle `dead` — a verdict about an endpoint nobody has configured yet.
    enqueued: list[dict[str, object]] = []
    monkeypatch.setattr(ai_models_route.run_model_health_check, "apply_async", lambda *a, **k: enqueued.append(k))
    caller = await create_user_service(db_session, email="autocheck-nokey@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models",
        json=_create_payload(name="No Key", model_alias="no-key", input_modalities=["text", "image"]),
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    # "Never checked", not "Dead" — the state the console renders for a row nobody has probed.
    assert response.json()["health_check_status"] is None
    assert enqueued == []


@pytest.mark.integration
async def test_create_for_an_out_of_band_credential_vendor_starts_a_check(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `aws_bedrock` has no `_PROVIDER_DEFAULT_KEY_FIELD` entry by design — litellm picks the
    # credential off the environment — so "no resolvable key" says nothing about whether the
    # endpoint can answer. Such a row is dispatchable and must not be silently skipped.
    enqueued: list[dict[str, object]] = []
    monkeypatch.setattr(ai_models_route.run_model_health_check, "apply_async", lambda *a, **k: enqueued.append(k))
    caller = await create_user_service(db_session, email="autocheck-bedrock@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models",
        json=_create_payload(
            name="Bedrock Vision",
            model_alias="bedrock-vision",
            provider="aws_bedrock",
            provider_model_id="anthropic.claude-3-5-sonnet-20240620-v1:0",
            input_modalities=["text", "image"],
        ),
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert enqueued == [{"args": [response.json()["id"]], "countdown": 1}]


@pytest.mark.integration
async def test_patch_of_a_legacy_generic_row_with_no_endpoint_starts_no_check(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `generic` is the other vendor missing from `_PROVIDER_DEFAULT_KEY_FIELD`, for the opposite
    # reason to `aws_bedrock`: it gets no platform credential at all. A legacy row predating the
    # endpoint rule stays patchable on purpose, and with no key and no endpoint there is genuinely
    # nothing to call — so this is the one case where "no key" is unambiguous.
    enqueued: list[dict[str, object]] = []
    monkeypatch.setattr(ai_models_route.run_model_health_check, "apply_async", lambda *a, **k: enqueued.append(k))
    caller = await create_user_service(db_session, email="autocheck-legacy@example.com", roles=[admin_role])
    model = await create_model_service(
        db_session,
        get_settings(),
        name="patch-legacy-generic",
        model_alias="patch-legacy-generic",
        provider=ProviderVendor.GENERIC,
        provider_model_id="m",
        inference_endpoint="http://legacy:8080/v1",
        input_modalities=[Modality.TEXT, Modality.IMAGE],
    )
    model.inference_endpoint = None  # the NULL a row predating the rule may still hold
    await db_session.flush()

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{model.id}",
        json={"provider_model_id": "m2"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert enqueued == []


@pytest.mark.integration
async def test_patch_survives_an_undecryptable_stored_key(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The autocheck must not decrypt to decide whether to run: an at-rest key rotation that dropped
    # the retired key would turn an unrelated PATCH into a 500 and `@transactional` would discard
    # the operator's edit — the one thing this side effect is not allowed to do.
    enqueued: list[dict[str, object]] = []
    monkeypatch.setattr(ai_models_route.run_model_health_check, "apply_async", lambda *a, **k: enqueued.append(k))
    caller = await create_user_service(db_session, email="autocheck-badkey@example.com", roles=[admin_role])
    model = await create_model_service(
        db_session,
        get_settings(),
        name="patch-badkey",
        model_alias="patch-badkey",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        input_modalities=[Modality.TEXT, Modality.IMAGE],
    )
    model.api_key_encrypted = "not-a-jwe"
    await db_session.flush()

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{model.id}",
        json={"provider_model_id": "gpt-4o-mini"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["provider_model_id"] == "gpt-4o-mini"
    # A stored credential counts as present without being read, so the check still runs.
    assert enqueued == [{"args": [str(model.id)], "countdown": 1}]


@pytest.mark.integration
async def test_create_without_image_starts_no_check(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing to contradict, so nothing to spend a provider call on.
    enqueued: list[dict[str, object]] = []
    monkeypatch.setattr(ai_models_route.run_model_health_check, "apply_async", lambda *a, **k: enqueued.append(k))
    caller = await create_user_service(db_session, email="autocheck-text@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models",
        json=_create_payload(),
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["health_check_status"] is None
    assert enqueued == []


@pytest.mark.integration
async def test_bulk_create_starts_no_checks(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `bulk_max_rows` is 1000; one paste must not become that many provider calls.
    enqueued: list[dict[str, object]] = []
    monkeypatch.setattr(ai_models_route.run_model_health_check, "apply_async", lambda *a, **k: enqueued.append(k))
    caller = await create_user_service(db_session, email="autocheck-bulk@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models/bulk",
        json={
            "rows": [
                _bulk_row("a", name="Bulk A", model_alias="bulk-a", input_modalities=["text", "image"]),
                _bulk_row("b", name="Bulk B", model_alias="bulk-b", input_modalities=["text", "image"]),
            ]
        },
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert enqueued == []


@pytest.mark.integration
async def test_patch_adding_image_starts_a_check(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueued: list[dict[str, object]] = []
    monkeypatch.setattr(ai_models_route.run_model_health_check, "apply_async", lambda *a, **k: enqueued.append(k))
    caller = await create_user_service(db_session, email="autocheck-patch@example.com", roles=[admin_role])
    model = await create_model_service(
        db_session,
        get_settings(),
        name="patch-declares-image",
        model_alias="patch-declares-image",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        api_key=SecretStr("sk-probe"),
    )

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{model.id}",
        json={"input_modalities": ["text", "image"]},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert enqueued == [{"args": [str(model.id)], "countdown": 1}]


@pytest.mark.integration
async def test_patch_resending_the_same_declaration_starts_no_check(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The console's edit form puts the whole body in every PATCH, `input_modalities` included, so a
    # trigger keyed on the field's presence would re-probe (and bill) on every rename.
    enqueued: list[dict[str, object]] = []
    monkeypatch.setattr(ai_models_route.run_model_health_check, "apply_async", lambda *a, **k: enqueued.append(k))
    caller = await create_user_service(db_session, email="autocheck-rename@example.com", roles=[admin_role])
    model = await create_model_service(
        db_session,
        get_settings(),
        name="patch-rename",
        model_alias="patch-rename",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        input_modalities=[Modality.TEXT, Modality.IMAGE],
    )

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{model.id}",
        json={"name": "Renamed", "input_modalities": ["text", "image"]},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert enqueued == []


@pytest.mark.integration
async def test_patch_moving_the_endpoint_rechecks_the_declaration(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same declaration, different endpoint — an unverified pair again, and any stored finding
    # describes the endpoint that is no longer there.
    enqueued: list[dict[str, object]] = []
    monkeypatch.setattr(ai_models_route.run_model_health_check, "apply_async", lambda *a, **k: enqueued.append(k))
    caller = await create_user_service(db_session, email="autocheck-endpoint@example.com", roles=[admin_role])
    model = await create_model_service(
        db_session,
        get_settings(),
        name="patch-endpoint",
        model_alias="patch-endpoint",
        provider=ProviderVendor.GENERIC,
        provider_model_id="m",
        inference_endpoint="http://old:8080/v1",
        input_modalities=[Modality.TEXT, Modality.IMAGE],
    )

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{model.id}",
        json={"inference_endpoint": "http://new:8080/v1"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert enqueued == [{"args": [str(model.id)], "countdown": 1}]


@pytest.mark.integration
async def test_patch_supersedes_a_check_already_in_flight(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Deferring to the running check would let it settle a finding for the declaration this PATCH
    # just replaced. `start_health_check` re-stamps the CAS token, so the older run loses on settle.
    enqueued: list[dict[str, object]] = []
    monkeypatch.setattr(ai_models_route.run_model_health_check, "apply_async", lambda *a, **k: enqueued.append(k))
    caller = await create_user_service(db_session, email="autocheck-inflight@example.com", roles=[admin_role])
    model = await create_model_service(
        db_session,
        get_settings(),
        name="patch-inflight",
        model_alias="patch-inflight",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        inference_endpoint="http://proxy:8080/v1",
    )
    await start_health_check_service(db_session, model)
    await db_session.commit()
    first_token = model.last_health_check_at
    assert first_token is not None

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{model.id}",
        json={"input_modalities": ["text", "image"]},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert enqueued == [{"args": [str(model.id)], "countdown": 1}]
    # A fresh token is what drops the superseded run's settle.
    await db_session.refresh(model)
    assert model.last_health_check_at != first_token


@pytest.mark.integration
async def test_reenabling_a_row_declaring_image_starts_a_check(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A disabled row is never probed (`_build_call` refuses to dispatch to one), so the re-enable is
    # the moment its declaration starts mattering — and the PATCH carries only `is_disabled`.
    enqueued: list[dict[str, object]] = []
    monkeypatch.setattr(ai_models_route.run_model_health_check, "apply_async", lambda *a, **k: enqueued.append(k))
    caller = await create_user_service(db_session, email="autocheck-reenable@example.com", roles=[admin_role])
    model = await create_model_service(
        db_session,
        get_settings(),
        name="patch-reenable",
        model_alias="patch-reenable",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        inference_endpoint="http://proxy:8080/v1",
        input_modalities=[Modality.TEXT, Modality.IMAGE],
        is_disabled=True,
    )

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{model.id}",
        json={"is_disabled": False},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert enqueued == [{"args": [str(model.id)], "countdown": 1}]


@pytest.mark.integration
async def test_disabling_a_row_starts_no_check(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the `False` direction is a trigger — the tripwire for `is_disabled in diff_after`.
    enqueued: list[dict[str, object]] = []
    monkeypatch.setattr(ai_models_route.run_model_health_check, "apply_async", lambda *a, **k: enqueued.append(k))
    caller = await create_user_service(db_session, email="autocheck-disable@example.com", roles=[admin_role])
    model = await create_model_service(
        db_session,
        get_settings(),
        name="patch-disable",
        model_alias="patch-disable",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        input_modalities=[Modality.TEXT, Modality.IMAGE],
    )

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{model.id}",
        json={"is_disabled": True},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert enqueued == []


@pytest.mark.integration
async def test_patch_changing_the_provider_model_id_rechecks_the_declaration(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The model id is part of the probed call, so moving it leaves an unverified combination.
    enqueued: list[dict[str, object]] = []
    monkeypatch.setattr(ai_models_route.run_model_health_check, "apply_async", lambda *a, **k: enqueued.append(k))
    caller = await create_user_service(db_session, email="autocheck-modelid@example.com", roles=[admin_role])
    model = await create_model_service(
        db_session,
        get_settings(),
        name="patch-model-id",
        model_alias="patch-model-id",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-3.5-turbo",
        inference_endpoint="http://proxy:8080/v1",
        input_modalities=[Modality.TEXT, Modality.IMAGE],
    )

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{model.id}",
        json={"provider_model_id": "gpt-4o"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert enqueued == [{"args": [str(model.id)], "countdown": 1}]


# Parametrised off `PROBED_CALL_FIELDS` itself, so a field added there with no move listed here
# fails on the lookup rather than going uncovered.
_PROBED_FIELD_MOVES: dict[str, Any] = {
    "input_modalities": [Modality.TEXT, Modality.IMAGE],
    "inference_endpoint": "http://proxy:8080/v1",
    "provider": ProviderVendor.ANTHROPIC,
    "provider_model_id": "gpt-4o-mini",
}


@pytest.mark.unit
@pytest.mark.parametrize("field", PROBED_CALL_FIELDS)
def test_the_audit_snapshot_renders_each_probed_field_move(field: str) -> None:
    # The trigger derives "did the probed call move?" from the audit diff, so it is only as good as
    # what the curated snapshot renders. Asserting the keys are present would stay green against a
    # snapshot that kept the key and coarsened the value past the point `changed_fields` can see the
    # move. The route's own condition is pinned by the integration tests above, not here.
    base: dict[str, Any] = {
        "name": "snapshot-coupling",
        "model_alias": "snapshot-coupling",
        "provider": ProviderVendor.OPENAI,
        "provider_model_id": "gpt-4o",
        "input_modalities": [Modality.TEXT],
        "inference_endpoint": None,
    }

    before = ai_models_route._ai_model_snapshot(AiModel(**base))
    after = ai_models_route._ai_model_snapshot(AiModel(**(base | {field: _PROBED_FIELD_MOVES[field]})))
    _, diff_after = changed_fields(before, after)

    assert set(PROBED_CALL_FIELDS) & diff_after.keys() == {field}


@pytest.mark.integration
async def test_create_survives_a_broker_outage(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The registry write is the operator's; the check is our side effect. Only one of them may fail,
    # and a row must not depend on Redis any more than on the provider.
    attempted: list[object] = []

    def _broker_down(*_a: object, **_k: object) -> None:
        attempted.append(None)
        raise OSError("redis unreachable")

    monkeypatch.setattr(ai_models_route.run_model_health_check, "apply_async", _broker_down)
    caller = await create_user_service(db_session, email="autocheck-broker@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models",
        json=_create_payload(
            name="Broker Row", model_alias="broker-row", input_modalities=["text", "image"], api_key="sk-probe"
        ),
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    # The enqueue has to have been reached, or the assertions below pass on a row that was never
    # a candidate for a check at all — which is what a payload with nothing to call would give.
    assert attempted
    stored = await db_session.execute(select(AiModel).where(col(AiModel.model_alias) == "broker-row"))
    row = stored.scalar_one_or_none()
    assert row is not None
    # And no stamp behind the missing task: `checking` here would read as a check in flight and
    # `check_in_flight` would answer the manual button with a silent 200 until the horizon passed.
    assert row.health_check_status is not HealthCheckStatus.CHECKING
    assert row.last_health_check_at is None


@pytest.mark.integration
async def test_create_disabled_starts_no_check(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Dispatch refuses a disabled row, so no red-teamer can hit the gap this closes; the manual
    # button stays for verifying an endpoint before enabling it.
    enqueued: list[dict[str, object]] = []
    monkeypatch.setattr(ai_models_route.run_model_health_check, "apply_async", lambda *a, **k: enqueued.append(k))
    caller = await create_user_service(db_session, email="autocheck-disabled@example.com", roles=[admin_role])

    response = await auth_db_client.post(
        "/api/v1/ai-models",
        json=_create_payload(
            name="Parked", model_alias="parked-image", input_modalities=["text", "image"], is_disabled=True
        ),
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["health_check_status"] is None
    assert enqueued == []
