"""Integration tests for the audit-log subsystem: service, endpoint, and instrumentation."""

from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import status
from fastapi.routing import APIRoute
from httpx import AsyncClient
from httpx import Response
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.api.v1.ai_models import _ai_model_snapshot
from app.api.v1.auth.users import _user_snapshot
from app.api.v1.evaluation_groups import _group_snapshot
from app.api.v1.evaluations import _assignment_snapshot
from app.api.v1.evaluations import _evaluation_snapshot
from app.api.v1.message_flags import _flag_snapshot
from app.api.v1.notes import _note_snapshot
from app.api.v1.organizations import _organization_snapshot
from app.api.v1.reviews import _review_snapshot
from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.annotations.enums import FlagStatus
from app.core.annotations.models import MessageFlag
from app.core.annotations.models import Note
from app.core.audit.enums import AuditAction
from app.core.audit.models import AuditLog
from app.core.audit.service import changed_fields
from app.core.audit.service import record_audit
from app.core.auth.models import EmailVerification
from app.core.auth.models import Invitation
from app.core.auth.models import InvitationStatus
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import grant_roles
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.auth.services.roles import create_role as create_role_service
from app.core.auth.services.tokens import generate_raw_token
from app.core.auth.services.tokens import hash_token
from app.core.auth.services.users import create_user as create_user_service
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import Conversation
from app.core.conversations.models import ConversationGroup
from app.core.conversations.models import Message as ConvMessage
from app.core.conversations.models import Turn
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import EvaluationStatus
from app.core.evaluations.enums import MetricsAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import EvaluationGroupAiModel
from app.core.evaluations.models import EvaluationTagKey
from app.core.evaluations.models import Scenario
from app.core.evaluations.models import Task
from app.core.exports.enums import ExportJobStatus
from app.core.exports.models import ExportJob
from app.core.licenses.catalog import DEFAULT_DATA_LICENSE_SPDX_ID
from app.core.licenses.catalog import curated_license_id
from app.core.middleware.audit import _LOGIN_ROUTE
from app.core.middleware.audit import _OIDC_CALLBACK_ROUTE
from app.core.middleware.audit import ACCESS_AUDIT_ROUTES
from app.core.middleware.audit import AUTH_LIFECYCLE_ROUTES
from app.core.middleware.audit import AuditAccessMiddleware
from app.core.middleware.audit import Message
from app.core.middleware.audit import Receive
from app.core.middleware.audit import Scope
from app.core.middleware.audit import Send
from app.core.organizations.models import Organization
from app.core.platform_settings.models import PLATFORM_SETTINGS_ID
from app.core.platform_settings.models import PlatformSettings
from app.core.reviews.enums import ReviewStatus
from app.core.reviews.models import Review
from app.main import app as fastapi_app
from tests.api.v1.conftest import make_token as _token
from tests.conftest import persist_evaluation_group
from tests.core.annotations.conftest import persist_conversation_target

pytestmark = pytest.mark.integration


@pytest.fixture
def audit_via_test_session(monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession) -> None:
    """Route the middleware's `standalone_session` to the test session.

    `AuditAccessMiddleware` writes access/login audits in a fresh `standalone_session`,
    which needs the lifespan-managed engine (absent under `ASGITransport`). Yield the
    test session instead (no commit — a `flush` makes the row visible in the same session).
    """

    @asynccontextmanager
    async def _fake_standalone() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr("app.core.middleware.audit.standalone_session", _fake_standalone)


@pytest_asyncio.fixture
async def admin_role(db_session: AsyncSession) -> Role:
    role = Role(
        name="auditor-admin",
        description="audit read + platform settings",
        permissions=[
            Permission.AUDIT_READ.value,
            Permission.PLATFORM_SETTINGS_READ.value,
            Permission.PLATFORM_SETTINGS_UPDATE.value,
        ],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def plain_role(db_session: AsyncSession) -> Role:
    role = Role(name="no-audit", description="no audit", permissions=[Permission.PLATFORM_SETTINGS_READ.value])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _caller(db_session: AsyncSession, role: Role, *, email: str) -> User:
    return await create_user_service(db_session, email=email, roles=[role])


def _assert_problem(response: Response, expected_status: int) -> None:
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == expected_status
    assert body["title"]


async def test_record_audit_writes_row(db_session: AsyncSession) -> None:
    actor = uuid4()
    target = uuid4()

    await record_audit(
        db_session,
        actor_id=actor,
        actor_email="a@example.com",
        action=AuditAction.EVALUATION_GROUP_PUBLISH,
        object_type="evaluation_group",
        object_id=target,
        before={"status": "approved"},
        after={"status": "published"},
    )

    row = (await db_session.execute(select(AuditLog).where(col(AuditLog.object_id) == target))).scalar_one()
    assert row.actor_id == actor
    assert row.actor_email == "a@example.com"
    assert row.action == "evaluation_group.publish"
    assert row.object_type == "evaluation_group"
    assert row.before == {"status": "approved"}
    assert row.after == {"status": "published"}
    assert row.context == {}  # default
    assert row.request_id is None  # no LoggingMiddleware in-test → contextvar unset


async def test_list_requires_admin(auth_db_client: AsyncClient, db_session: AsyncSession, plain_role: Role) -> None:
    caller = await _caller(db_session, plain_role, email="noperm@example.com")

    response = await auth_db_client.get("/api/v1/audit-logs", headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_403_FORBIDDEN
    _assert_problem(response, status.HTTP_403_FORBIDDEN)


async def test_list_returns_rows_filtered_and_ordered(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    admin = await _caller(db_session, admin_role, email="admin@example.com")
    g1 = uuid4()
    await record_audit(
        db_session,
        actor_id=admin.id,
        actor_email=admin.email,
        action=AuditAction.EVALUATION_GROUP_PUBLISH,
        object_type="evaluation_group",
        object_id=g1,
    )
    await record_audit(
        db_session,
        actor_id=admin.id,
        actor_email=admin.email,
        action=AuditAction.USER_DELETE,
        object_type="user",
        object_id=uuid4(),
    )

    all_rows = await auth_db_client.get("/api/v1/audit-logs", headers=_auth(_token(admin)))
    by_action = await auth_db_client.get(
        "/api/v1/audit-logs?action=evaluation_group.publish", headers=_auth(_token(admin))
    )

    assert all_rows.status_code == status.HTTP_200_OK
    assert all_rows.json()["total"] >= 2
    assert by_action.json()["total"] == 1
    item = by_action.json()["items"][0]
    assert item["action"] == "evaluation_group.publish"
    assert item["object_id"] == str(g1)


async def test_list_rejects_oversized_limit(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    admin = await _caller(db_session, admin_role, email="page@example.com")

    response = await auth_db_client.get("/api/v1/audit-logs?limit=101", headers=_auth(_token(admin)))

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_failed_login_is_audited_with_no_details(
    auth_db_client: AsyncClient, db_session: AsyncSession, audit_via_test_session: None
) -> None:
    # A failed login is intercepted by the middleware (never the auth handler, so the audit
    # can't block a login). The row records the *attempt* only — no email, actor, or password.
    response = await auth_db_client.post(
        "/api/v1/auth/login", json={"email": "ghost@example.com", "password": "wrong-password-1234"}
    )

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    db_session.expire_all()
    row = (
        await db_session.execute(select(AuditLog).where(col(AuditLog.action) == AuditAction.AUTH_LOGIN_FAILED.value))
    ).scalar_one()
    assert row.actor_id is None
    assert row.actor_email is None
    assert row.object_type is None
    assert row.before is None
    assert row.after is None
    assert row.context == {}
    # Neither the attempted email nor the password may appear anywhere in the row.
    haystack = str(row.context) + str(row.actor_email) + str(row.before) + str(row.after)
    assert "ghost@example.com" not in haystack
    assert "wrong-password-1234" not in haystack


async def _drive_access_middleware(
    *,
    method: str,
    route_name: str,
    status_code: int,
    path_params: dict[str, str] | None = None,
    actor: SessionUser | None = None,
    audit_subject: UUID | None = None,
    audit_action: AuditAction | None = None,
) -> None:
    """Run `AuditAccessMiddleware` over a synthetic ASGI request (its real code path).

    The scope + stub downstream app are the middleware's genuine inputs; only the audit
    write is redirected to the test session by the `audit_via_test_session` fixture.
    """

    async def stub_app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": status_code, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        return None

    state: dict[str, object] = {}
    if actor is not None:
        state["user"] = actor
    if audit_subject is not None:
        state["audit_subject"] = audit_subject  # what a lifecycle handler stashes on request.state
    if audit_action is not None:
        state["audit_action"] = audit_action  # what the OIDC callback handler stashes on request.state
    scope: Scope = {
        "type": "http",
        "method": method,
        "route": SimpleNamespace(name=route_name),
        "path_params": path_params or {},
        "state": state,
    }
    await AuditAccessMiddleware(stub_app)(scope, receive, send)


def _session_user(email: str) -> SessionUser:
    return SessionUser(id=uuid4(), email=email, email_verified=True, first_name=None, last_name=None, provider="local")


async def test_access_middleware_audits_export_download(db_session: AsyncSession, audit_via_test_session: None) -> None:
    actor = _session_user("admin@example.com")
    job_id = uuid4()

    await _drive_access_middleware(
        method="GET",
        route_name="download_export_job_endpoint",
        status_code=status.HTTP_200_OK,
        path_params={"job_id": str(job_id)},
        actor=actor,
    )

    row = (
        await db_session.execute(select(AuditLog).where(col(AuditLog.action) == AuditAction.EXPORT_DOWNLOAD.value))
    ).scalar_one()
    assert row.actor_id == actor.id
    assert row.actor_email == "admin@example.com"
    assert row.object_type == "export_job"
    assert row.object_id == job_id
    assert row.before is None
    assert row.after is None


async def test_access_middleware_skips_non_success_status(
    db_session: AsyncSession, audit_via_test_session: None
) -> None:
    await _drive_access_middleware(
        method="GET",
        route_name="download_export_job_endpoint",
        status_code=status.HTTP_404_NOT_FOUND,
        path_params={"job_id": str(uuid4())},
        actor=_session_user("a@example.com"),
    )

    count = (
        await db_session.execute(
            select(func.count()).select_from(AuditLog).where(col(AuditLog.action) == AuditAction.EXPORT_DOWNLOAD.value)
        )
    ).scalar_one()
    assert count == 0


async def test_access_middleware_ignores_unlisted_route(db_session: AsyncSession, audit_via_test_session: None) -> None:
    before = (await db_session.execute(select(func.count()).select_from(AuditLog))).scalar_one()

    await _drive_access_middleware(
        method="GET",
        route_name="list_users_endpoint",
        status_code=status.HTTP_200_OK,
        actor=_session_user("a@example.com"),
    )

    after = (await db_session.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    assert after == before


async def test_access_middleware_audits_login_success_without_details(
    db_session: AsyncSession, audit_via_test_session: None
) -> None:
    await _drive_access_middleware(method="POST", route_name="login", status_code=status.HTTP_200_OK)

    row = (
        await db_session.execute(select(AuditLog).where(col(AuditLog.action) == AuditAction.AUTH_LOGIN.value))
    ).scalar_one()
    assert row.actor_id is None
    assert row.actor_email is None
    assert row.object_type is None
    assert row.context == {}


async def test_access_middleware_audits_oidc_callback_success(
    db_session: AsyncSession, audit_via_test_session: None
) -> None:
    subject = uuid4()

    await _drive_access_middleware(
        method="GET",
        route_name="auth_callback",
        status_code=status.HTTP_302_FOUND,  # every outcome the handler's own logic reaches is a 302
        audit_subject=subject,
        audit_action=AuditAction.AUTH_LOGIN,
    )

    row = (
        await db_session.execute(select(AuditLog).where(col(AuditLog.action) == AuditAction.AUTH_LOGIN.value))
    ).scalar_one()
    assert row.object_type == "user"
    assert row.object_id == subject
    assert row.actor_id is None


async def test_access_middleware_ignores_oidc_callback_with_no_stashed_action(
    db_session: AsyncSession, audit_via_test_session: None
) -> None:
    # No `audit_action` stashed (e.g. `access_denied`/`invalid_claims` — deliberately
    # out of scope, same call as `_audit_login` skipping non-credential-outcome statuses).
    before = (await db_session.execute(select(func.count()).select_from(AuditLog))).scalar_one()

    await _drive_access_middleware(method="GET", route_name="auth_callback", status_code=status.HTTP_302_FOUND)

    after = (await db_session.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    assert after == before


@pytest.mark.unit  # pure function — belongs in the unit suite, not the DB/integration run
def test_audit_targets_resolve_to_a_single_route() -> None:
    # The middleware keys audits on (method, endpoint function name), so a renamed handler
    # silently STOPS auditing — and a second handler sharing that (method, name) would
    # silently CHANGE what's audited (e.g. a future POST route also named `login`). Guard
    # both: every access + login target must match a live route, and match exactly one.
    # Routers are included lazily (`_IncludedRouter`), so descend recursively rather than
    # assuming a flat `app.routes`.
    def collect(routes: list[Any], acc: list[APIRoute]) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                acc.append(route)
            nested = getattr(route, "routes", None)
            original = getattr(route, "original_router", None)
            if nested:
                collect(nested, acc)
            if original is not None and getattr(original, "routes", None):
                collect(original.routes, acc)

    api_routes: list[APIRoute] = []
    collect(fastapi_app.routes, api_routes)
    routed: Counter[tuple[str, str]] = Counter(
        (method, route.name) for route in api_routes for method in (route.methods or ())
    )
    for target in [*ACCESS_AUDIT_ROUTES, _LOGIN_ROUTE, *AUTH_LIFECYCLE_ROUTES, _OIDC_CALLBACK_ROUTE]:
        assert routed[target] == 1, f"audit target {target} matches {routed[target]} routes (want exactly 1)"


async def test_platform_settings_update_is_audited_with_before_after(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    admin = await _caller(db_session, admin_role, email="settings@example.com")
    admin_id = admin.id  # capture before the PATCH commit expires the ORM object
    token = _token(admin)
    # Seed a starting license DIFFERENT from the target, so `before != after` and the
    # early-snapshot logic is actually exercised (otherwise both sides read the default).
    starting_id = curated_license_id("CC-BY-SA-4.0")
    default_id = curated_license_id(DEFAULT_DATA_LICENSE_SPDX_ID)
    assert starting_id != default_id
    db_session.add(PlatformSettings(id=PLATFORM_SETTINGS_ID, default_license_id=starting_id))
    await db_session.flush()

    response = await auth_db_client.patch(
        "/api/v1/platform-settings",
        json={"default_license_id": str(default_id)},
        headers=_auth(token),
    )

    assert response.status_code == status.HTTP_200_OK
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(AuditLog).where(col(AuditLog.action) == AuditAction.PLATFORM_SETTINGS_UPDATE.value)
        )
    ).scalar_one()
    assert row.actor_id == admin_id
    assert row.object_type == "platform_settings"
    assert row.object_id == PLATFORM_SETTINGS_ID
    assert row.before == {"default_license_id": str(starting_id)}
    assert row.after == {"default_license_id": str(default_id)}


async def test_platform_settings_invite_only_patch_audits_only_that_field(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    admin = await _caller(db_session, admin_role, email="inviteaudit@example.com")
    token = _token(admin)

    response = await auth_db_client.patch(
        "/api/v1/platform-settings",
        json={"invite_only": True},
        headers=_auth(token),
    )

    assert response.status_code == status.HTTP_200_OK
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(AuditLog).where(col(AuditLog.action) == AuditAction.PLATFORM_SETTINGS_UPDATE.value)
        )
    ).scalar_one()
    assert row.before == {"invite_only": False}
    assert row.after == {"invite_only": True}


async def test_platform_settings_ttl_patch_audits_only_that_field(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    # Guards the audit snapshot covering the knob — a missing key would mean an empty diff
    # and no audit row for a real change.
    admin = await _caller(db_session, admin_role, email="ttlaudit@example.com")
    token = _token(admin)

    response = await auth_db_client.patch(
        "/api/v1/platform-settings",
        json={"email_verification_ttl_hours": 48},
        headers=_auth(token),
    )

    assert response.status_code == status.HTTP_200_OK
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(AuditLog).where(col(AuditLog.action) == AuditAction.PLATFORM_SETTINGS_UPDATE.value)
        )
    ).scalar_one()
    assert row.before == {"email_verification_ttl_hours": 24}
    assert row.after == {"email_verification_ttl_hours": 48}


async def test_platform_settings_noop_patch_leaves_no_audit_row(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    # Values equal to the effective (still-transient) settings — nothing is written or audited.
    admin = await _caller(db_session, admin_role, email="noopaudit@example.com")
    token = _token(admin)

    response = await auth_db_client.patch(
        "/api/v1/platform-settings",
        json={"invite_only": False, "default_license_id": str(curated_license_id(DEFAULT_DATA_LICENSE_SPDX_ID))},
        headers=_auth(token),
    )

    assert response.status_code == status.HTTP_200_OK
    db_session.expire_all()
    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(col(AuditLog.action) == AuditAction.PLATFORM_SETTINGS_UPDATE.value)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.unit  # pure function — belongs in the unit suite, not the DB/integration run
def test_user_snapshot_captures_role_only_change() -> None:
    # A role-only PATCH (privilege escalation) must be visible in the audit diff — the whole
    # point of the trail. Regression: _user_snapshot once omitted roles, so changed_fields
    # returned an empty before/after for exactly this change.
    user = User(
        email="u@example.com",
        first_name="A",
        last_name="B",
        status=UserStatus.ACTIVE,
        roles=[Role(name="red_teamer")],
    )
    before = _user_snapshot(user)
    user.roles = [Role(name="admin"), Role(name="red_teamer")]
    after = _user_snapshot(user)

    diff_before, diff_after = changed_fields(before, after)
    assert diff_before["roles"] == ["red_teamer"]
    assert diff_after["roles"] == ["admin", "red_teamer"]


@pytest.mark.unit  # pure function — belongs in the unit suite, not the DB/integration run
def test_changed_fields_reports_a_key_dropped_from_after() -> None:
    # A key present in `before` but absent from `after` must read as a change (keys are the
    # union of both sides), not be silently ignored — the latent trap an after-only iteration
    # would leave for a future snapshot that drops a key.
    diff_before, diff_after = changed_fields({"a": 1, "b": 2}, {"a": 1})
    assert diff_before == {"b": 2}
    assert diff_after == {"b": None}


async def test_audit_row_rolls_back_with_the_action(db_session: AsyncSession) -> None:
    # Atomicity: an audit row written then not committed (session rolled back) leaves nothing.
    before = (await db_session.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    async with db_session.begin_nested() as savepoint:
        await record_audit(
            db_session,
            actor_id=uuid4(),
            actor_email="x@example.com",
            action=AuditAction.USER_UPDATE,
            object_type="user",
            object_id=uuid4(),
        )
        await savepoint.rollback()
    after = (await db_session.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    assert after == before


async def _conversation_owned_by(db_session: AsyncSession, owner: User) -> tuple[UUID, UUID]:
    """Build the minimal live graph for a caller-owned conversation → `(evaluation_id, conversation_id)`.

    `persist_evaluation_group` grants the owner the in-group `owner` role and defaults to a
    public/approved group, so the conversation resolves as visible to its owner.
    """
    group = await persist_evaluation_group(db_session, created_by_id=owner.id)
    evaluation = Evaluation(title="Eval", description="d", evaluation_group_id=group.id, created_by_id=owner.id)
    db_session.add(evaluation)
    await db_session.flush()
    alias = f"m-{uuid4().hex[:8]}"
    model = AiModel(name=alias, model_alias=alias, provider=ProviderVendor.ANTHROPIC, provider_model_id=f"{alias}-id")
    db_session.add(model)
    await db_session.flush()
    scenario = Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0)
    db_session.add(scenario)
    await db_session.flush()
    assignment = EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id)
    conv_group = ConversationGroup(user_id=owner.id, evaluation_id=evaluation.id, name="g", scenario_id=scenario.id)
    db_session.add(assignment)
    db_session.add(conv_group)
    await db_session.flush()
    conversation = Conversation(
        user_id=owner.id,
        evaluation_id=evaluation.id,
        evaluation_ai_model_id=assignment.id,
        conversation_group_id=conv_group.id,
        scenario_id=scenario.id,
    )
    db_session.add(conversation)
    await db_session.flush()
    await db_session.refresh(conversation)
    return evaluation.id, conversation.id


async def test_data_read_captures_actor_through_real_auth_middleware(
    auth_db_client: AsyncClient, db_session: AsyncSession, audit_via_test_session: None
) -> None:
    # E2E proof of the one runtime-fragile assumption the synthetic-scope tests can't exercise:
    # AuthMiddleware sets `request.state.user` on the scope, and the inner AuditAccessMiddleware
    # reads it back off the same scope after the response. Drive a real allowlisted transcript
    # read through the full stack and assert the actor lands on the audit row.
    reader_role = Role(
        name="conv-reader", description="conversations read", permissions=[Permission.CONVERSATIONS_READ.value]
    )
    db_session.add(reader_role)
    await db_session.flush()
    caller = await _caller(db_session, reader_role, email="reader@example.com")
    caller_id = caller.id  # capture before expire_all
    evaluation_id, conversation_id = await _conversation_owned_by(db_session, caller)

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation_id}/conversations/{conversation_id}/messages",
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(AuditLog).where(
                col(AuditLog.action) == AuditAction.DATA_READ.value,
                col(AuditLog.object_id) == conversation_id,
            )
        )
    ).scalar_one()
    assert row.actor_id == caller_id
    assert row.actor_email == "reader@example.com"
    assert row.object_type == "conversation"


# --------------------------------------------------------------------------------------
# Engagement / group mutation instrumentation
# --------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def group_mutator_role(db_session: AsyncSession) -> Role:
    """Group CRUD + transitions + the `evaluation_groups:manage` break-glass (so group,
    member and invitation mutations authorize without an in-group role) + audit read."""
    role = Role(
        name="group-mutator",
        description="evaluation-groups rw + manage + audit",
        permissions=[
            Permission.EVALUATION_GROUPS_READ.value,
            Permission.EVALUATION_GROUPS_CREATE.value,
            Permission.EVALUATION_GROUPS_UPDATE.value,
            Permission.EVALUATION_GROUPS_MANAGE.value,
            Permission.EVALUATION_GROUPS_MANAGE_MEMBERS.value,
            Permission.AUDIT_READ.value,
        ],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


async def _a_group(
    db_session: AsyncSession,
    *,
    status: PublicationStatus = PublicationStatus.APPROVED,
    access_level: EvaluationGroupAccessLevel = EvaluationGroupAccessLevel.PUBLIC,
    title: str = "Engagement",
) -> EvaluationGroup:
    # Future start_date, one allowed model and one playable evaluation so both
    # publication gates pass: submit (start_date not before today, ≥1 model) and
    # publish (≥1 evaluation, each with a scenario).
    group = await persist_evaluation_group(
        db_session, status=status, access_level=access_level, title=title, start_date=date(2027, 1, 1)
    )
    alias = f"m-{uuid4().hex[:8]}"
    model = AiModel(name=alias, model_alias=alias, provider=ProviderVendor.ANTHROPIC, provider_model_id=f"{alias}-id")
    db_session.add(model)
    await db_session.flush()
    db_session.add(EvaluationGroupAiModel(evaluation_group_id=group.id, model_id=model.id))
    await db_session.flush()
    evaluation = Evaluation(title="E", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(evaluation)
    await db_session.flush()
    db_session.add(Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0))
    await db_session.flush()
    return group


async def _a_member(db_session: AsyncSession, group: EvaluationGroup, role: Role, *, email: str) -> User:
    """A user who is an in-group member holding `role`."""
    user = await _caller(db_session, role, email=email)
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group.id, user.id, [role])
    return user


@pytest.mark.unit  # pure function — belongs in the unit suite, not the DB/integration run
def test_group_snapshot_stringifies_enums_dates_and_ids_for_json() -> None:
    # access_level/status are StrEnums, start/end_date are `date`, organization_id a UUID —
    # none is JSONB-safe as-is; the snapshot must emit strings/None. Also pins the keyset.
    org_id = uuid4()
    group = EvaluationGroup(
        title="T",
        description="d",
        created_by_id=uuid4(),
        organization_id=org_id,
        access_level=EvaluationGroupAccessLevel.ORGANIZATION,
        status=PublicationStatus.DRAFT,
        metrics_access_during=MetricsAccessLevel.ALL_MEMBERS,
        metrics_access_after=MetricsAccessLevel.OWNER_ONLY,
        start_date=date(2027, 1, 1),
        end_date=date(2027, 2, 1),
    )
    snapshot = _group_snapshot(group)
    assert snapshot["access_level"] == "organization"
    assert snapshot["status"] == "draft"
    assert snapshot["metrics_access_during"] == "all_members"
    assert snapshot["metrics_access_after"] == "owner_only"
    assert snapshot["organization_id"] == str(org_id)
    assert snapshot["start_date"] == "2027-01-01"
    assert snapshot["end_date"] == "2027-02-01"
    assert set(snapshot) == {
        "title",
        "description",
        "access_level",
        "status",
        "metrics_access_during",
        "metrics_access_after",
        "organization_id",
        "start_date",
        "end_date",
        "data_license_id",
    }


async def test_group_create_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, group_mutator_role: Role, system_roles: dict[str, Role]
) -> None:
    caller = await _caller(db_session, group_mutator_role, email="grp-create@example.com")
    caller_id = caller.id  # capture before expire_all
    alias = f"m-{uuid4().hex[:8]}"
    model = AiModel(name=alias, model_alias=alias, provider=ProviderVendor.ANTHROPIC, provider_model_id=f"{alias}-id")
    db_session.add(model)
    await db_session.flush()

    response = await auth_db_client.post(
        "/api/v1/evaluation-groups",
        json={
            "title": "Engagement",
            "description": "d",
            "access_level": "public",
            "start_date": "2027-01-01",
            "allowed_model_ids": [str(model.id)],
        },
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_201_CREATED
    row = await _audit_row(db_session, AuditAction.EVALUATION_GROUP_CREATE)
    assert row.actor_id == caller_id
    assert row.object_type == "evaluation_group"
    assert row.object_id == UUID(response.json()["id"])
    assert row.before is None
    assert row.after is not None
    assert row.after["title"] == "Engagement"
    assert row.after["status"] == str(PublicationStatus.PENDING_APPROVAL)


async def test_group_draft_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, group_mutator_role: Role, system_roles: dict[str, Role]
) -> None:
    caller = await _caller(db_session, group_mutator_role, email="grp-draft@example.com")

    response = await auth_db_client.post(
        "/api/v1/evaluation-groups/draft", json={"title": "WIP"}, headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_201_CREATED
    row = await _audit_row(db_session, AuditAction.EVALUATION_GROUP_DRAFT)
    assert row.before is None
    assert row.after is not None
    assert row.after["title"] == "WIP"
    assert row.after["status"] == str(PublicationStatus.DRAFT)


async def test_group_duplicate_is_audited_with_source_context(
    auth_db_client: AsyncClient, db_session: AsyncSession, group_mutator_role: Role
) -> None:
    caller = await _caller(db_session, group_mutator_role, email="grp-dup@example.com")
    group = await _a_group(db_session, title="Source")
    group_id = group.id  # capture before expire_all

    response = await auth_db_client.post(
        f"/api/v1/evaluation-groups/{group_id}/duplicate", headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_201_CREATED
    new_id = UUID(response.json()["id"])
    row = await _audit_row(db_session, AuditAction.EVALUATION_GROUP_DUPLICATE)
    assert row.object_id == new_id
    assert row.after is not None
    assert row.after["status"] == str(PublicationStatus.DRAFT)
    assert row.context["source_group_id"] == str(group_id)


async def test_group_update_is_audited_with_diff(
    auth_db_client: AsyncClient, db_session: AsyncSession, group_mutator_role: Role
) -> None:
    caller = await _caller(db_session, group_mutator_role, email="grp-update@example.com")
    group = await _a_group(db_session, title="Before")
    group_id = group.id  # capture before expire_all

    response = await auth_db_client.patch(
        f"/api/v1/evaluation-groups/{group_id}", json={"title": "After"}, headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_200_OK
    row = await _audit_row(db_session, AuditAction.EVALUATION_GROUP_UPDATE)
    assert row.object_id == group_id
    assert row.before == {"title": "Before"}
    assert row.after == {"title": "After"}


async def test_metrics_only_group_update_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, group_mutator_role: Role
) -> None:
    # metrics_access_* is a real editable field but was absent from `_group_snapshot`, so a
    # metrics-only PATCH diffed empty and the no-op guard dropped the row entirely — a genuine
    # mutation to who can see metrics went unaudited. The snapshot must cover it.
    caller = await _caller(db_session, group_mutator_role, email="grp-metrics@example.com")
    group = await _a_group(db_session)  # seeded metrics_access_during == owner_only
    group_id = group.id  # capture before expire_all

    response = await auth_db_client.patch(
        f"/api/v1/evaluation-groups/{group_id}",
        json={"metrics_access_during": "all_members"},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    row = await _audit_row(db_session, AuditAction.EVALUATION_GROUP_UPDATE)
    assert row.object_id == group_id
    assert row.before == {"metrics_access_during": "owner_only"}
    assert row.after == {"metrics_access_during": "all_members"}


@pytest.mark.parametrize(
    ("seed_status", "path", "action", "after_status"),
    [
        (PublicationStatus.DRAFT, "submit", AuditAction.EVALUATION_GROUP_SUBMIT, PublicationStatus.PENDING_APPROVAL),
        (PublicationStatus.APPROVED, "publish", AuditAction.EVALUATION_GROUP_PUBLISH, PublicationStatus.PUBLISHED),
        (PublicationStatus.PUBLISHED, "finish", AuditAction.EVALUATION_GROUP_FINISH, PublicationStatus.INACTIVE),
        (
            PublicationStatus.PENDING_APPROVAL,
            "approve",
            AuditAction.EVALUATION_GROUP_APPROVE,
            PublicationStatus.APPROVED,
        ),
        (
            PublicationStatus.PENDING_APPROVAL,
            "request-changes",
            AuditAction.EVALUATION_GROUP_REQUEST_CHANGES,
            PublicationStatus.CHANGES_REQUESTED,
        ),
    ],
)
async def test_group_transition_is_audited_with_status(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    group_mutator_role: Role,
    seed_status: PublicationStatus,
    path: str,
    action: AuditAction,
    after_status: PublicationStatus,
) -> None:
    caller = await _caller(db_session, group_mutator_role, email=f"grp-{path}@example.com")
    group = await _a_group(db_session, status=seed_status)
    group_id = group.id  # capture before expire_all

    response = await auth_db_client.post(f"/api/v1/evaluation-groups/{group_id}/{path}", headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_200_OK
    row = await _audit_row(db_session, action)
    assert row.object_id == group_id
    assert row.before == {"status": str(seed_status)}
    assert row.after == {"status": str(after_status)}


async def test_group_reject_is_audited_with_reason(
    auth_db_client: AsyncClient, db_session: AsyncSession, group_mutator_role: Role
) -> None:
    caller = await _caller(db_session, group_mutator_role, email="grp-reject@example.com")
    group = await _a_group(db_session, status=PublicationStatus.PENDING_APPROVAL)
    group_id = group.id  # capture before expire_all

    response = await auth_db_client.post(
        f"/api/v1/evaluation-groups/{group_id}/reject",
        json={"rejection_reason": "Out of scope"},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    row = await _audit_row(db_session, AuditAction.EVALUATION_GROUP_REJECT)
    assert row.before == {"status": str(PublicationStatus.PENDING_APPROVAL)}
    assert row.after is not None
    assert row.after["status"] == str(PublicationStatus.NOT_APPROVED)
    assert row.after["rejection_reason"] == "Out of scope"


async def test_group_join_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, group_mutator_role: Role
) -> None:
    caller = await _caller(db_session, group_mutator_role, email="grp-join@example.com")
    caller_id = caller.id  # capture before expire_all
    group = await _a_group(db_session, status=PublicationStatus.PUBLISHED)
    group_id = group.id

    response = await auth_db_client.post(f"/api/v1/evaluation-groups/{group_id}/join", headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_201_CREATED
    row = await _audit_row(db_session, AuditAction.EVALUATION_GROUP_JOIN)
    assert row.object_id == group_id
    assert row.after == {"roles": ["red_teamer"], "user_id": str(caller_id)}


async def test_member_add_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, group_mutator_role: Role, system_roles: dict[str, Role]
) -> None:
    caller = await _caller(db_session, group_mutator_role, email="mem-add@example.com")
    group = await _a_group(db_session)
    group_id = group.id  # capture before expire_all
    target = await _caller(db_session, group_mutator_role, email="target-add@example.com")
    target_id = target.id
    red_id = system_roles["red_teamer"].id

    response = await auth_db_client.post(
        f"/api/v1/evaluation-groups/{group_id}/members",
        json={"user_id": str(target_id), "role_ids": [str(red_id)]},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_201_CREATED
    row = await _audit_row(db_session, AuditAction.MEMBER_ADD)
    assert row.object_type == "evaluation_group_member"
    assert row.object_id == target_id
    assert row.after == {"roles": ["red_teamer"]}
    assert row.context["group_id"] == str(group_id)


async def test_member_set_roles_is_audited_with_diff(
    auth_db_client: AsyncClient, db_session: AsyncSession, group_mutator_role: Role, system_roles: dict[str, Role]
) -> None:
    caller = await _caller(db_session, group_mutator_role, email="mem-set@example.com")
    group = await _a_group(db_session)
    group_id = group.id  # capture before expire_all
    target = await _a_member(db_session, group, system_roles["red_teamer"], email="target-set@example.com")
    target_id = target.id
    annotator_id = system_roles["annotator"].id

    response = await auth_db_client.patch(
        f"/api/v1/evaluation-groups/{group_id}/members/{target_id}",
        json={"role_ids": [str(annotator_id)]},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    row = await _audit_row(db_session, AuditAction.MEMBER_SET_ROLES)
    assert row.object_id == target_id
    assert row.before == {"roles": ["red_teamer"]}
    assert row.after == {"roles": ["annotator"]}


async def test_noop_member_set_roles_writes_no_audit_row(
    auth_db_client: AsyncClient, db_session: AsyncSession, group_mutator_role: Role, system_roles: dict[str, Role]
) -> None:
    caller = await _caller(db_session, group_mutator_role, email="mem-noop@example.com")
    group = await _a_group(db_session)
    group_id = group.id  # capture before expire_all
    target = await _a_member(db_session, group, system_roles["red_teamer"], email="target-noop@example.com")
    target_id = target.id
    red_id = system_roles["red_teamer"].id

    # Replace with the identical role set: a no-op that must write no audit row, mirroring the
    # group PATCH's diff guard (test_noop_group_update_writes_no_audit_row).
    response = await auth_db_client.patch(
        f"/api/v1/evaluation-groups/{group_id}/members/{target_id}",
        json={"role_ids": [str(red_id)]},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    db_session.expire_all()
    count = (
        await db_session.execute(
            select(func.count()).select_from(AuditLog).where(col(AuditLog.action) == AuditAction.MEMBER_SET_ROLES.value)
        )
    ).scalar_one()
    assert count == 0


async def test_member_set_roles_retaining_a_hidden_role_writes_no_audit_row(
    auth_db_client: AsyncClient, db_session: AsyncSession, group_mutator_role: Role, system_roles: dict[str, Role]
) -> None:
    # The member also holds a deactivated role, which the projection hides and the replace
    # retains. Nothing changed, so the row would claim a revocation that never happened.
    caller = await _caller(db_session, group_mutator_role, email="mem-hidden@example.com")
    group = await _a_group(db_session)
    group_id = group.id  # capture before expire_all
    target = await _a_member(db_session, group, system_roles["red_teamer"], email="target-hidden@example.com")
    target_id = target.id
    red_id = system_roles["red_teamer"].id
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group_id, target_id, [system_roles["annotator"]])
    system_roles["annotator"].is_active = False
    db_session.add(system_roles["annotator"])
    await db_session.flush()

    response = await auth_db_client.patch(
        f"/api/v1/evaluation-groups/{group_id}/members/{target_id}",
        json={"role_ids": [str(red_id)]},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    assert [role["name"] for role in response.json()["roles"]] == ["red_teamer"]
    db_session.expire_all()
    count = (
        await db_session.execute(
            select(func.count()).select_from(AuditLog).where(col(AuditLog.action) == AuditAction.MEMBER_SET_ROLES.value)
        )
    ).scalar_one()
    assert count == 0


async def test_member_remove_is_audited_before_only(
    auth_db_client: AsyncClient, db_session: AsyncSession, group_mutator_role: Role, system_roles: dict[str, Role]
) -> None:
    caller = await _caller(db_session, group_mutator_role, email="mem-rm@example.com")
    group = await _a_group(db_session)
    group_id = group.id  # capture before expire_all
    target = await _a_member(db_session, group, system_roles["red_teamer"], email="target-rm@example.com")
    target_id = target.id

    response = await auth_db_client.delete(
        f"/api/v1/evaluation-groups/{group_id}/members/{target_id}", headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    row = await _audit_row(db_session, AuditAction.MEMBER_REMOVE)
    assert row.object_id == target_id
    assert row.before == {"roles": ["red_teamer"]}
    assert row.after is None
    assert row.context["group_id"] == str(group_id)


async def test_bulk_invitations_are_audited_per_row(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    group_mutator_role: Role,
    system_roles: dict[str, Role],
    celery_enqueue_stub: MagicMock,
) -> None:
    caller = await _caller(db_session, group_mutator_role, email="bulk-inviter@example.com")
    group = await _a_group(db_session)
    group_id = group.id  # capture before expire_all
    red_id = str(system_roles["red_teamer"].id)

    response = await auth_db_client.post(
        f"/api/v1/evaluation-groups/{group_id}/invitations/bulk",
        json={
            "rows": [
                {"row_key": "a", "data": {"email": "a@example.com", "role_ids": [red_id]}},
                {"row_key": "b", "data": {"email": "b@example.com", "role_ids": [red_id]}},
            ]
        },
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    db_session.expire_all()
    rows = (
        (
            await db_session.execute(
                select(AuditLog)
                .where(col(AuditLog.action) == AuditAction.INVITATION_CREATE.value)
                .order_by(col(AuditLog.created_at))
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    # Payload shape, not just the count — the single-invite audit test that used to own
    # these assertions went away with its endpoint.
    assert {row.object_type for row in rows} == {"evaluation_group_invitation"}
    assert {row.context["group_id"] for row in rows} == {str(group_id)}
    assert [row.after for row in rows] == [
        {"email": "a@example.com", "outcome": "invited", "roles": ["red_teamer"]},
        {"email": "b@example.com", "outcome": "invited", "roles": ["red_teamer"]},
    ]


async def test_bulk_invitations_dry_run_writes_no_audit(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    group_mutator_role: Role,
    system_roles: dict[str, Role],
    celery_enqueue_stub: MagicMock,
) -> None:
    caller = await _caller(db_session, group_mutator_role, email="bulk-dry@example.com")
    group = await _a_group(db_session)
    group_id = group.id  # capture before expire_all
    red_id = str(system_roles["red_teamer"].id)

    response = await auth_db_client.post(
        f"/api/v1/evaluation-groups/{group_id}/invitations/bulk",
        json={"rows": [{"row_key": "a", "data": {"email": "a@example.com", "role_ids": [red_id]}}], "dry_run": True},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    db_session.expire_all()
    count = (
        await db_session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(col(AuditLog.action) == AuditAction.INVITATION_CREATE.value)
        )
    ).scalar_one()
    assert count == 0


async def test_failed_group_transition_writes_no_audit_row(
    auth_db_client: AsyncClient, db_session: AsyncSession, group_mutator_role: Role
) -> None:
    # record_audit sits AFTER the transition service, which 409s on a wrong source state
    # (publish requires `approved`), so a rejected transition must leave no phantom row.
    caller = await _caller(db_session, group_mutator_role, email="grp-bad@example.com")
    group = await _a_group(db_session, status=PublicationStatus.DRAFT)
    group_id = group.id

    response = await auth_db_client.post(f"/api/v1/evaluation-groups/{group_id}/publish", headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_409_CONFLICT
    db_session.expire_all()
    count = (
        await db_session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(col(AuditLog.action) == AuditAction.EVALUATION_GROUP_PUBLISH.value)
        )
    ).scalar_one()
    assert count == 0


async def test_bulk_invitations_audit_only_the_rows_that_commit(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    group_mutator_role: Role,
    system_roles: dict[str, Role],
    celery_enqueue_stub: MagicMock,
) -> None:
    # Per-row isolation: a row whose processor raises an APIError (here an unknown role id ->
    # 400, which fires BEFORE record_audit) contributes no audit row, while the sibling row
    # still commits one. (The flush-then-savepoint-rollback path — record_audit flushes a row,
    # then a later same-row failure discards it — is not reachable today: nothing in the
    # processor fails after record_audit, and dry_run writes no rows because the
    # `if not payload.dry_run` guard skips record_audit outright, not via savepoint rollback.)
    caller = await _caller(db_session, group_mutator_role, email="bulk-partial@example.com")
    group = await _a_group(db_session)
    group_id = group.id  # capture before expire_all
    red_id = str(system_roles["red_teamer"].id)

    response = await auth_db_client.post(
        f"/api/v1/evaluation-groups/{group_id}/invitations/bulk",
        json={
            "rows": [
                {"row_key": "ok", "data": {"email": "ok@example.com", "role_ids": [red_id]}},
                {"row_key": "bad", "data": {"email": "bad@example.com", "role_ids": [str(uuid4())]}},
            ]
        },
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert Counter(row["status"] for row in body["results"]) == {"ok": 1, "failed": 1}
    db_session.expire_all()
    count = (
        await db_session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(col(AuditLog.action) == AuditAction.INVITATION_CREATE.value)
        )
    ).scalar_one()
    assert count == 1


async def test_noop_group_update_writes_no_audit_row(
    auth_db_client: AsyncClient, db_session: AsyncSession, group_mutator_role: Role
) -> None:
    # A PATCH that matches current state changes nothing, so `changed_fields` is empty —
    # the handler must skip the row rather than log a misleading update with empty diffs.
    caller = await _caller(db_session, group_mutator_role, email="grp-noop@example.com")
    group = await _a_group(db_session, title="Unchanged")
    group_id = group.id  # capture before expire_all

    response = await auth_db_client.patch(
        f"/api/v1/evaluation-groups/{group_id}", json={"title": "Unchanged"}, headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_200_OK
    db_session.expire_all()
    count = (
        await db_session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(col(AuditLog.action) == AuditAction.EVALUATION_GROUP_UPDATE.value)
        )
    ).scalar_one()
    assert count == 0


async def _audit_row(db_session: AsyncSession, action: AuditAction) -> AuditLog:
    """The single audit row for ``action`` (append-only; one per test)."""
    db_session.expire_all()
    result = await db_session.execute(select(AuditLog).where(col(AuditLog.action) == action.value))
    return result.scalar_one()


# --------------------------------------------------------------------------------------
# Evaluation-content mutation instrumentation
# --------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def mutator_role(db_session: AsyncSession) -> Role:
    """Evaluations CRUD + approve + the `evaluation_groups:manage` break-glass (so mutations
    authorize without an in-group role) + audit read."""
    role = Role(
        name="eval-mutator",
        description="evaluations rw + manage + audit",
        permissions=[
            Permission.EVALUATIONS_READ.value,
            Permission.EVALUATIONS_CREATE.value,
            Permission.EVALUATIONS_UPDATE.value,
            Permission.EVALUATIONS_DELETE.value,
            Permission.EVALUATIONS_APPROVE.value,
            Permission.EVALUATION_GROUPS_MANAGE.value,
            Permission.AUDIT_READ.value,
        ],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


async def _an_evaluation(db_session: AsyncSession, **kwargs: Any) -> Evaluation:
    group = await persist_evaluation_group(db_session)
    row = Evaluation(
        title=kwargs.pop("title", "Eval"),
        description=kwargs.pop("description", "d"),
        evaluation_group_id=group.id,
        created_by_id=group.created_by_id,
        **kwargs,
    )
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


async def _an_ai_model(db_session: AsyncSession) -> AiModel:
    alias = f"m-{uuid4().hex[:8]}"
    model = AiModel(name=alias, model_alias=alias, provider=ProviderVendor.ANTHROPIC, provider_model_id=f"{alias}-id")
    db_session.add(model)
    await db_session.flush()
    await db_session.refresh(model)
    return model


async def _an_assignment(
    db_session: AsyncSession, evaluation_id: UUID, model_id: UUID, *, mask: str | None = None
) -> EvaluationAiModel:
    assignment = EvaluationAiModel(evaluation_id=evaluation_id, model_id=model_id, model_display_mask=mask)
    db_session.add(assignment)
    await db_session.flush()
    await db_session.refresh(assignment)
    return assignment


async def _a_scenario(db_session: AsyncSession, evaluation_id: UUID, *, name: str = "S", position: int = 0) -> Scenario:
    row = Scenario(name=name, description="d", evaluation_id=evaluation_id, position=position, required_reviews=1)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


async def _a_task(db_session: AsyncSession, scenario_id: UUID, *, name: str = "T") -> Task:
    row = Task(name=name, description="d", scenario_id=scenario_id)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


async def test_evaluation_create_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, mutator_role: Role
) -> None:
    caller = await _caller(db_session, mutator_role, email="ev-create@example.com")
    caller_id = caller.id  # capture before expire_all
    group = await persist_evaluation_group(db_session)

    response = await auth_db_client.post(
        "/api/v1/evaluations",
        json={"title": "Gauntlet", "description": "d", "evaluation_group_id": str(group.id)},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_201_CREATED
    row = await _audit_row(db_session, AuditAction.EVALUATION_CREATE)
    assert row.actor_id == caller_id
    assert row.actor_email == "ev-create@example.com"  # actor attribution, not just the id
    assert row.object_type == "evaluation"
    assert row.object_id == UUID(response.json()["id"])
    assert row.before is None
    assert row.after is not None
    assert row.after["title"] == "Gauntlet"


async def test_evaluation_update_is_audited_with_diff(
    auth_db_client: AsyncClient, db_session: AsyncSession, mutator_role: Role
) -> None:
    caller = await _caller(db_session, mutator_role, email="ev-update@example.com")
    evaluation = await _an_evaluation(db_session, title="Before")
    evaluation_id = evaluation.id  # capture before expire_all

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation_id}", json={"title": "After"}, headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_200_OK
    row = await _audit_row(db_session, AuditAction.EVALUATION_UPDATE)
    assert row.object_id == evaluation_id
    assert row.before == {"title": "Before"}
    assert row.after == {"title": "After"}


async def test_evaluation_delete_is_audited_before_only(
    auth_db_client: AsyncClient, db_session: AsyncSession, mutator_role: Role
) -> None:
    caller = await _caller(db_session, mutator_role, email="ev-delete@example.com")
    evaluation = await _an_evaluation(db_session, title="Doomed")
    evaluation_id = evaluation.id  # capture before expire_all

    response = await auth_db_client.delete(f"/api/v1/evaluations/{evaluation_id}", headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_204_NO_CONTENT
    row = await _audit_row(db_session, AuditAction.EVALUATION_DELETE)
    assert row.object_id == evaluation_id
    assert row.before is not None
    assert row.before["title"] == "Doomed"
    assert row.after is None


async def test_evaluation_duplicate_is_audited_with_source_context(
    auth_db_client: AsyncClient, db_session: AsyncSession, mutator_role: Role
) -> None:
    caller = await _caller(db_session, mutator_role, email="ev-dup@example.com")
    evaluation = await _an_evaluation(db_session, title="Source")
    evaluation_id = evaluation.id  # capture before expire_all

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation_id}/duplicate", headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_201_CREATED
    new_id = UUID(response.json()["id"])
    row = await _audit_row(db_session, AuditAction.EVALUATION_DUPLICATE)
    assert row.object_id == new_id
    assert row.context["source_evaluation_id"] == str(evaluation_id)


async def test_model_assign_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, mutator_role: Role
) -> None:
    caller = await _caller(db_session, mutator_role, email="assign@example.com")
    evaluation = await _an_evaluation(db_session)
    model = await _an_ai_model(db_session)
    # The parent group must allow the model (assign enforces the subset).
    db_session.add(EvaluationGroupAiModel(evaluation_group_id=evaluation.evaluation_group_id, model_id=model.id))
    await db_session.flush()
    evaluation_id, model_id = evaluation.id, model.id  # capture before expire_all

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation_id}/models",
        json={"model_id": str(model_id)},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_201_CREATED
    row = await _audit_row(db_session, AuditAction.EVALUATION_MODEL_ASSIGN)
    assert row.object_type == "evaluation_ai_model"
    assert row.object_id == UUID(response.json()["id"])
    assert row.after is not None
    assert row.after["model_id"] == str(model_id)
    assert row.context["evaluation_id"] == str(evaluation_id)


async def test_model_update_is_audited_with_diff(
    auth_db_client: AsyncClient, db_session: AsyncSession, mutator_role: Role
) -> None:
    caller = await _caller(db_session, mutator_role, email="assign-update@example.com")
    evaluation = await _an_evaluation(db_session)
    model = await _an_ai_model(db_session)
    assignment = await _an_assignment(db_session, evaluation.id, model.id, mask="Old")

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/models/{assignment.id}",
        json={"model_display_mask": "New"},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    row = await _audit_row(db_session, AuditAction.EVALUATION_MODEL_UPDATE)
    assert row.before == {"model_display_mask": "Old"}
    assert row.after == {"model_display_mask": "New"}


async def test_model_unassign_is_audited_before_only(
    auth_db_client: AsyncClient, db_session: AsyncSession, mutator_role: Role
) -> None:
    caller = await _caller(db_session, mutator_role, email="unassign@example.com")
    evaluation = await _an_evaluation(db_session)
    model = await _an_ai_model(db_session)
    assignment = await _an_assignment(db_session, evaluation.id, model.id)
    evaluation_id, model_id, assignment_id = evaluation.id, model.id, assignment.id  # capture before expire_all

    response = await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation_id}/models/{assignment_id}", headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    row = await _audit_row(db_session, AuditAction.EVALUATION_MODEL_UNASSIGN)
    assert row.object_id == assignment_id
    assert row.before is not None
    assert row.before["model_id"] == str(model_id)
    assert row.after is None


async def test_evaluation_approve_is_audited_with_status(
    auth_db_client: AsyncClient, db_session: AsyncSession, mutator_role: Role
) -> None:
    caller = await _caller(db_session, mutator_role, email="approve@example.com")
    evaluation = await _an_evaluation(db_session, status=EvaluationStatus.UNDER_REVIEW)

    response = await auth_db_client.post(f"/api/v1/evaluations/{evaluation.id}/approve", headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_200_OK
    row = await _audit_row(db_session, AuditAction.EVALUATION_APPROVE)
    assert row.before == {"status": str(EvaluationStatus.UNDER_REVIEW)}
    assert row.after == {"status": str(EvaluationStatus.APPROVED)}


async def test_evaluation_reject_is_audited_with_reason(
    auth_db_client: AsyncClient, db_session: AsyncSession, mutator_role: Role
) -> None:
    caller = await _caller(db_session, mutator_role, email="reject@example.com")
    evaluation = await _an_evaluation(db_session, status=EvaluationStatus.UNDER_REVIEW)

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/reject",
        json={"rejection_reason": "Out of scope"},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    row = await _audit_row(db_session, AuditAction.EVALUATION_REJECT)
    assert row.before == {"status": str(EvaluationStatus.UNDER_REVIEW)}
    assert row.after is not None
    assert row.after["status"] == str(EvaluationStatus.REJECTED)
    assert row.after["rejection_reason"] == "Out of scope"


async def test_failed_transition_writes_no_audit_row(
    auth_db_client: AsyncClient, db_session: AsyncSession, mutator_role: Role
) -> None:
    # record_audit rides the handler's transaction and sits AFTER the service call, so a
    # rejected transition (approve on a DRAFT evaluation → 409) must leave no phantom row.
    caller = await _caller(db_session, mutator_role, email="bad-transition@example.com")
    evaluation = await _an_evaluation(db_session, status=EvaluationStatus.DRAFT)

    response = await auth_db_client.post(f"/api/v1/evaluations/{evaluation.id}/approve", headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_409_CONFLICT
    db_session.expire_all()
    result = await db_session.execute(
        select(func.count()).select_from(AuditLog).where(col(AuditLog.action) == AuditAction.EVALUATION_APPROVE.value)
    )
    assert result.scalar_one() == 0


async def test_scenario_create_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, mutator_role: Role
) -> None:
    caller = await _caller(db_session, mutator_role, email="sc-create@example.com")
    evaluation = await _an_evaluation(db_session)
    evaluation_id = evaluation.id  # capture before expire_all

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation_id}/scenarios",
        json={"name": "Injection", "description": "d"},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_201_CREATED
    row = await _audit_row(db_session, AuditAction.SCENARIO_CREATE)
    assert row.actor_email == "sc-create@example.com"  # actor attribution, not just the id
    assert row.object_type == "scenario"
    assert row.object_id == UUID(response.json()["id"])
    assert row.after is not None
    assert row.after["name"] == "Injection"
    assert row.context["evaluation_id"] == str(evaluation_id)


async def test_scenario_update_is_audited_with_diff(
    auth_db_client: AsyncClient, db_session: AsyncSession, mutator_role: Role
) -> None:
    caller = await _caller(db_session, mutator_role, email="sc-update@example.com")
    evaluation = await _an_evaluation(db_session)
    scenario = await _a_scenario(db_session, evaluation.id, name="Before")

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/scenarios/{scenario.id}",
        json={"name": "After"},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    row = await _audit_row(db_session, AuditAction.SCENARIO_UPDATE)
    assert row.before == {"name": "Before"}
    assert row.after == {"name": "After"}


async def test_scenario_delete_is_audited_before_only(
    auth_db_client: AsyncClient, db_session: AsyncSession, mutator_role: Role
) -> None:
    caller = await _caller(db_session, mutator_role, email="sc-delete@example.com")
    evaluation = await _an_evaluation(db_session)
    scenario = await _a_scenario(db_session, evaluation.id, name="Doomed")

    response = await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation.id}/scenarios/{scenario.id}", headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    row = await _audit_row(db_session, AuditAction.SCENARIO_DELETE)
    assert row.before is not None
    assert row.before["name"] == "Doomed"
    assert row.after is None


async def test_scenario_reorder_is_audited_on_the_evaluation(
    auth_db_client: AsyncClient, db_session: AsyncSession, mutator_role: Role
) -> None:
    caller = await _caller(db_session, mutator_role, email="sc-reorder@example.com")
    evaluation = await _an_evaluation(db_session)
    s1 = await _a_scenario(db_session, evaluation.id, name="one", position=0)
    s2 = await _a_scenario(db_session, evaluation.id, name="two", position=1)
    evaluation_id, s1_id, s2_id = evaluation.id, s1.id, s2.id  # capture before expire_all

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation_id}/scenarios/order",
        json={"scenario_ids": [str(s2_id), str(s1_id)]},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    row = await _audit_row(db_session, AuditAction.SCENARIO_REORDER)
    assert row.object_type == "evaluation"
    assert row.object_id == evaluation_id
    assert row.after == {"order": [str(s2_id), str(s1_id)]}


async def test_task_create_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, mutator_role: Role
) -> None:
    caller = await _caller(db_session, mutator_role, email="tk-create@example.com")
    evaluation = await _an_evaluation(db_session)
    scenario = await _a_scenario(db_session, evaluation.id)
    scenario_id = scenario.id  # capture before expire_all

    response = await auth_db_client.post(
        f"/api/v1/scenarios/{scenario_id}/tasks",
        json={"name": "Reveal", "description": "d"},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_201_CREATED
    row = await _audit_row(db_session, AuditAction.TASK_CREATE)
    assert row.actor_email == "tk-create@example.com"  # actor attribution, not just the id
    assert row.object_type == "task"
    assert row.object_id == UUID(response.json()["id"])
    assert row.after is not None
    assert row.after["name"] == "Reveal"
    assert row.context["scenario_id"] == str(scenario_id)


async def test_task_update_is_audited_with_diff(
    auth_db_client: AsyncClient, db_session: AsyncSession, mutator_role: Role
) -> None:
    caller = await _caller(db_session, mutator_role, email="tk-update@example.com")
    evaluation = await _an_evaluation(db_session)
    scenario = await _a_scenario(db_session, evaluation.id)
    task = await _a_task(db_session, scenario.id, name="Before")

    response = await auth_db_client.patch(
        f"/api/v1/scenarios/{scenario.id}/tasks/{task.id}",
        json={"name": "After"},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    row = await _audit_row(db_session, AuditAction.TASK_UPDATE)
    assert row.before == {"name": "Before"}
    assert row.after == {"name": "After"}


async def test_task_delete_is_audited_before_only(
    auth_db_client: AsyncClient, db_session: AsyncSession, mutator_role: Role
) -> None:
    caller = await _caller(db_session, mutator_role, email="tk-delete@example.com")
    evaluation = await _an_evaluation(db_session)
    scenario = await _a_scenario(db_session, evaluation.id)
    task = await _a_task(db_session, scenario.id, name="Doomed")

    response = await auth_db_client.delete(
        f"/api/v1/scenarios/{scenario.id}/tasks/{task.id}", headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    row = await _audit_row(db_session, AuditAction.TASK_DELETE)
    assert row.before is not None
    assert row.before["name"] == "Doomed"
    assert row.after is None


@pytest.mark.unit  # pure function — belongs in the unit suite, not the DB/integration run
def test_evaluation_snapshot_stringifies_status_for_json() -> None:
    # The status enum must land as a plain string — a StrEnum member isn't JSONB-safe and a
    # changed_fields diff would compare enum-vs-string on read-back. Also pins the curated keyset.
    evaluation = Evaluation(
        title="T",
        description="d",
        evaluation_group_id=uuid4(),
        created_by_id=uuid4(),
        status=EvaluationStatus.DRAFT,
    )
    snapshot = _evaluation_snapshot(evaluation)
    assert snapshot["status"] == str(EvaluationStatus.DRAFT)
    assert isinstance(snapshot["status"], str)
    assert set(snapshot) == {
        "title",
        "description",
        "status",
        "mask_models_enabled",
        # Governance flags belong in the snapshot: without them, flipping tagging off or changing
        # the restriction — which decides what reaches the model — audits as `before={} after={}`.
        "tags_enabled",
        "tags_restricted",
        "data_license_id",
        "cover_image",
    }


@pytest.mark.unit  # pure function — belongs in the unit suite, not the DB/integration run
def test_assignment_snapshot_stringifies_model_id_for_json() -> None:
    # model_id is a UUID; the audit JSONB column needs a string, and object_id lookups compare
    # against the stringified form.
    model_id = uuid4()
    assignment = EvaluationAiModel(evaluation_id=uuid4(), model_id=model_id, model_display_mask="M")
    snapshot = _assignment_snapshot(assignment)
    assert snapshot["model_id"] == str(model_id)
    assert set(snapshot) == {"model_id", "model_display_mask", "parameters"}


@pytest_asyncio.fixture
async def org_admin_role(db_session: AsyncSession) -> Role:
    """Full organizations CRUD + member management + audit read (organizations are admin-only)."""
    role = Role(
        name="org-admin",
        description="organizations crud + members + audit",
        permissions=[
            Permission.ORGANIZATIONS_READ.value,
            Permission.ORGANIZATIONS_CREATE.value,
            Permission.ORGANIZATIONS_UPDATE.value,
            Permission.ORGANIZATIONS_DELETE.value,
            Permission.ORGANIZATIONS_MANAGE_MEMBERS.value,
            Permission.AUDIT_READ.value,
        ],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


async def _an_org(db_session: AsyncSession, *, name: str = "Acme", description: str | None = "d") -> Organization:
    org = Organization(name=name, description=description)
    db_session.add(org)
    await db_session.flush()
    await db_session.refresh(org)
    return org


async def _one_audit(db_session: AsyncSession, action: AuditAction) -> AuditLog:
    """The single audit row for ``action`` (append-only; one per test)."""
    db_session.expire_all()
    return (await db_session.execute(select(AuditLog).where(col(AuditLog.action) == action.value))).scalar_one()


@pytest.mark.unit  # pure function — belongs in the unit suite, not the DB/integration run
def test_organization_snapshot_curated_fields() -> None:
    # Curated to name + description; pins the keyset (no stray columns, no secrets to exclude here).
    org = Organization(name="Acme", description="d")
    assert _organization_snapshot(org) == {"name": "Acme", "description": "d"}


async def test_organization_create_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, org_admin_role: Role
) -> None:
    caller = await _caller(db_session, org_admin_role, email="org-create@example.com")
    caller_id = caller.id  # capture before expire_all

    response = await auth_db_client.post(
        "/api/v1/organizations", json={"name": "Acme", "description": "d"}, headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_201_CREATED
    row = await _one_audit(db_session, AuditAction.ORGANIZATION_CREATE)
    assert row.actor_id == caller_id
    assert row.object_type == "organization"
    assert row.object_id == UUID(response.json()["id"])
    assert row.before is None
    assert row.after == {"name": "Acme", "description": "d"}


async def test_organization_update_is_audited_with_diff(
    auth_db_client: AsyncClient, db_session: AsyncSession, org_admin_role: Role
) -> None:
    caller = await _caller(db_session, org_admin_role, email="org-update@example.com")
    org = await _an_org(db_session, name="Before", description="old")
    org_id = org.id  # capture before expire_all

    response = await auth_db_client.patch(
        f"/api/v1/organizations/{org_id}", json={"name": "After"}, headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_200_OK
    row = await _one_audit(db_session, AuditAction.ORGANIZATION_UPDATE)
    assert row.object_id == org_id
    assert row.before == {"name": "Before"}
    assert row.after == {"name": "After"}


async def test_noop_organization_update_writes_no_audit_row(
    auth_db_client: AsyncClient, db_session: AsyncSession, org_admin_role: Role
) -> None:
    # A PATCH matching current state changes nothing → empty diff → no misleading row.
    caller = await _caller(db_session, org_admin_role, email="org-noop@example.com")
    org = await _an_org(db_session, name="Same", description="d")
    org_id = org.id  # capture before expire_all

    response = await auth_db_client.patch(
        f"/api/v1/organizations/{org_id}", json={"name": "Same"}, headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_200_OK
    db_session.expire_all()
    count = (
        await db_session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(col(AuditLog.action) == AuditAction.ORGANIZATION_UPDATE.value)
        )
    ).scalar_one()
    assert count == 0


async def test_organization_delete_is_audited_before_only(
    auth_db_client: AsyncClient, db_session: AsyncSession, org_admin_role: Role
) -> None:
    caller = await _caller(db_session, org_admin_role, email="org-delete@example.com")
    org = await _an_org(db_session, name="Doomed", description="d")
    org_id = org.id  # capture before expire_all

    response = await auth_db_client.delete(f"/api/v1/organizations/{org_id}", headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_204_NO_CONTENT
    row = await _one_audit(db_session, AuditAction.ORGANIZATION_DELETE)
    assert row.object_id == org_id
    assert row.before == {"name": "Doomed", "description": "d"}
    assert row.after is None


async def test_organization_member_add_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, org_admin_role: Role
) -> None:
    caller = await _caller(db_session, org_admin_role, email="org-mem-add@example.com")
    org = await _an_org(db_session)
    org_id = org.id  # capture before expire_all
    target = await _caller(db_session, org_admin_role, email="org-target-add@example.com")
    target_id = target.id

    response = await auth_db_client.post(
        f"/api/v1/organizations/{org_id}/members",
        json={"user_id": str(target_id)},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    row = await _one_audit(db_session, AuditAction.ORGANIZATION_MEMBER_ADD)
    assert row.object_type == "user"
    assert row.object_id == target_id
    assert row.before == {"organization_id": None}
    assert row.after == {"organization_id": str(org_id)}
    assert row.context["organization_id"] == str(org_id)


async def test_organization_member_remove_is_audited_with_org_cleared(
    auth_db_client: AsyncClient, db_session: AsyncSession, org_admin_role: Role
) -> None:
    caller = await _caller(db_session, org_admin_role, email="org-mem-rm@example.com")
    org = await _an_org(db_session)
    org_id = org.id  # capture before expire_all
    target = await _caller(db_session, org_admin_role, email="org-target-rm@example.com")
    target_id = target.id
    target.organization_id = org_id  # seed membership
    db_session.add(target)
    await db_session.flush()

    response = await auth_db_client.delete(
        f"/api/v1/organizations/{org_id}/members/{target_id}", headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    row = await _one_audit(db_session, AuditAction.ORGANIZATION_MEMBER_REMOVE)
    assert row.object_type == "user"
    assert row.object_id == target_id
    # Symmetric with member_add: the FK transition is org -> None, not a before-only snapshot.
    assert row.before == {"organization_id": str(org_id)}
    assert row.after == {"organization_id": None}
    assert row.context["organization_id"] == str(org_id)


# ai-models: create/update/delete + credential set/clear (event-only, never the key) + bulk


@pytest_asyncio.fixture
async def model_admin_role(db_session: AsyncSession) -> Role:
    """Full ai-model registry CRUD + credential rotation + audit read."""
    role = Role(
        name="model-admin",
        description="ai-models crud + credentials + audit",
        permissions=[
            Permission.MODELS_READ.value,
            Permission.MODELS_CREATE.value,
            Permission.MODELS_UPDATE.value,
            Permission.MODELS_DELETE.value,
            Permission.AUDIT_READ.value,
        ],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


async def _a_model(
    db_session: AsyncSession,
    *,
    name: str = "GPT",
    provider: ProviderVendor = ProviderVendor.OPENAI,
    api_key_encrypted: str | None = None,
) -> AiModel:
    model = AiModel(
        name=name,
        model_alias=f"{name.lower()}-alias",
        provider=provider,
        provider_model_id=f"{name.lower()}-id",
        api_key_encrypted=api_key_encrypted,
    )
    db_session.add(model)
    await db_session.flush()
    await db_session.refresh(model)
    return model


@pytest.mark.unit  # pure function — belongs in the unit suite, not the DB/integration run
def test_ai_model_snapshot_excludes_api_key() -> None:
    # The snapshot must carry the curated identity/config fields and NEVER any credential —
    # neither the plaintext `api_key` nor the stored `api_key_encrypted` ciphertext.
    model = AiModel(
        name="M",
        model_alias="m",
        provider=ProviderVendor.OPENAI,
        provider_model_id="pid",
        api_key_encrypted="CIPHER-XYZ",
    )
    snapshot = _ai_model_snapshot(model)
    assert set(snapshot) == {
        "name",
        "description",
        "model_alias",
        "provider",
        "input_modalities",
        "output_modalities",
        "provider_model_id",
        "endpoint_name",
        "inference_endpoint",
        "is_disabled",
        "warmup_enabled",
        "advanced_params_disabled",
        "inactivity_alert_hours",
        "icon_file",
        "labels",
    }
    assert snapshot["provider"] == "openai"
    assert snapshot["is_disabled"] is False
    assert "api_key" not in snapshot
    assert "api_key_encrypted" not in snapshot
    assert "CIPHER-XYZ" not in str(snapshot)


async def test_model_create_is_audited_without_secret(
    auth_db_client: AsyncClient, db_session: AsyncSession, model_admin_role: Role
) -> None:
    caller = await _caller(db_session, model_admin_role, email="model-create@example.com")
    caller_id = caller.id  # capture before expire_all

    response = await auth_db_client.post(
        "/api/v1/ai-models",
        json={
            "name": "GPT",
            "model_alias": "gpt-x",
            "provider": "openai",
            "provider_model_id": "gpt-4o",
            "api_key": "sk-secret-PLAINTEXT-123",
        },
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_201_CREATED
    row = await _one_audit(db_session, AuditAction.AI_MODEL_CREATE)
    assert row.actor_id == caller_id
    assert row.object_type == "ai_model"
    assert row.object_id == UUID(response.json()["id"])
    assert row.after is not None
    assert row.after["name"] == "GPT"
    assert row.after["provider"] == "openai"
    # secret-leak guard: no api_key field, and the plaintext key nowhere in the row.
    assert "api_key" not in row.after
    haystack = str(row.after) + str(row.before) + str(row.context)
    assert "sk-secret-PLAINTEXT-123" not in haystack


async def test_ai_model_update_is_audited_with_diff(
    auth_db_client: AsyncClient, db_session: AsyncSession, model_admin_role: Role
) -> None:
    caller = await _caller(db_session, model_admin_role, email="model-update@example.com")
    model = await _a_model(db_session, name="Before")
    model_id = model.id  # capture before expire_all

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{model_id}", json={"name": "After"}, headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_200_OK
    row = await _one_audit(db_session, AuditAction.AI_MODEL_UPDATE)
    assert row.object_id == model_id
    assert row.before == {"name": "Before"}
    assert row.after == {"name": "After"}


async def test_ai_model_update_description_only_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, model_admin_role: Role
) -> None:
    # The snapshot is a curated field list, so a change the list omits is dropped by
    # `changed_fields` and the trail reports nothing at all.
    caller = await _caller(db_session, model_admin_role, email="model-note@example.com")
    model = await _a_model(db_session, name="NoteM")
    model_id = model.id  # capture before expire_all

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{model_id}",
        json={"description": "Client Acme only."},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    row = await _one_audit(db_session, AuditAction.AI_MODEL_UPDATE)
    assert row.object_id == model_id
    assert row.before == {"description": None}
    assert row.after == {"description": "Client Acme only."}


async def test_ai_model_clearing_the_description_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, model_admin_role: Role
) -> None:
    # Wiping an engagement restriction is the direction that matters for the trail, and a
    # snapshot that skipped falsy `after` values would leave it unrecorded.
    caller = await _caller(db_session, model_admin_role, email="model-note-clear@example.com")
    model = await _a_model(db_session, name="NoteClearM")
    model.description = "Client Acme only."
    db_session.add(model)
    await db_session.flush()
    model_id = model.id  # capture before expire_all

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{model_id}",
        json={"description": None},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    row = await _one_audit(db_session, AuditAction.AI_MODEL_UPDATE)
    assert row.before == {"description": "Client Acme only."}
    assert row.after == {"description": None}


async def test_ai_model_update_icon_only_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, model_admin_role: Role
) -> None:
    # icon_file is PATCH-editable and now in the curated snapshot, so a change to it alone is audited.
    caller = await _caller(db_session, model_admin_role, email="model-icon@example.com")
    model = await _a_model(db_session, name="IconM")
    model_id = model.id  # capture before expire_all

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{model_id}", json={"icon_file": "logo.png"}, headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_200_OK
    row = await _one_audit(db_session, AuditAction.AI_MODEL_UPDATE)
    assert row.object_id == model_id
    assert row.before == {"icon_file": None}
    assert row.after == {"icon_file": "logo.png"}


async def test_ai_model_update_extras_only_is_audited_event_only(
    auth_db_client: AsyncClient, db_session: AsyncSession, model_admin_role: Role
) -> None:
    # extras carries auth/call-shape metadata kept out of the trail, so a PATCH touching only it
    # records the *event* (before/after None) rather than nothing — its values never enter the row.
    caller = await _caller(db_session, model_admin_role, email="model-extras@example.com")
    model = await _a_model(db_session, name="ExtrasM")
    model_id = model.id  # capture before expire_all

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{model_id}",
        json={"extras": {"api_version": "2024-05-01-preview"}},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    row = await _one_audit(db_session, AuditAction.AI_MODEL_UPDATE)
    assert row.object_id == model_id
    assert row.before is None
    assert row.after is None
    # The event-only row names which opaque field moved (no values).
    assert row.context == {"opaque_fields_changed": ["extras"]}
    assert "2024-05-01-preview" not in str(row.before) + str(row.after) + str(row.context)


async def test_ai_model_update_curated_and_extras_records_single_curated_row(
    auth_db_client: AsyncClient, db_session: AsyncSession, model_admin_role: Role
) -> None:
    # A PATCH changing BOTH a curated field and extras records ONE row via the curated branch —
    # the event-only elif must not double-fire — but that row must still SIGNAL the opaque change
    # (trail fidelity); extras values still never enter the row.
    caller = await _caller(db_session, model_admin_role, email="model-combined@example.com")
    model = await _a_model(db_session, name="CombinedBefore")
    model_id = model.id  # capture before expire_all

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{model_id}",
        json={"name": "CombinedAfter", "extras": {"api_version": "2024-05-01-preview"}},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    db_session.expire_all()
    count = (
        await db_session.execute(
            select(func.count()).select_from(AuditLog).where(col(AuditLog.action) == AuditAction.AI_MODEL_UPDATE.value)
        )
    ).scalar_one()
    assert count == 1
    row = await _one_audit(db_session, AuditAction.AI_MODEL_UPDATE)
    assert row.before == {"name": "CombinedBefore"}
    assert row.after == {"name": "CombinedAfter"}
    # Fidelity: the curated-diff row also flags that extras moved (names only, no values).
    assert row.context == {"opaque_fields_changed": ["extras"]}
    assert "2024-05-01-preview" not in str(row.before) + str(row.after) + str(row.context)


async def test_ai_model_update_noop_extras_writes_no_audit_row(
    auth_db_client: AsyncClient, db_session: AsyncSession, model_admin_role: Role
) -> None:
    # A no-op extras PATCH (payload equals the stored value) changes nothing — no row, not even
    # event-only. Guards the value-compare against a refactor to key on field-presence.
    caller = await _caller(db_session, model_admin_role, email="model-extras-noop@example.com")
    model = await _a_model(db_session, name="NoopM")
    model.extras = {"api_version": "2024-05-01-preview"}
    db_session.add(model)
    await db_session.flush()
    model_id = model.id  # capture before expire_all

    response = await auth_db_client.patch(
        f"/api/v1/ai-models/{model_id}",
        json={"extras": {"api_version": "2024-05-01-preview"}},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    db_session.expire_all()
    count = (
        await db_session.execute(
            select(func.count()).select_from(AuditLog).where(col(AuditLog.action) == AuditAction.AI_MODEL_UPDATE.value)
        )
    ).scalar_one()
    assert count == 0


async def test_model_delete_is_audited_before_only(
    auth_db_client: AsyncClient, db_session: AsyncSession, model_admin_role: Role
) -> None:
    caller = await _caller(db_session, model_admin_role, email="model-delete@example.com")
    model = await _a_model(db_session, name="Doomed")
    model_id = model.id  # capture before expire_all

    response = await auth_db_client.delete(f"/api/v1/ai-models/{model_id}", headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_204_NO_CONTENT
    row = await _one_audit(db_session, AuditAction.AI_MODEL_DELETE)
    assert row.object_id == model_id
    assert row.before is not None
    assert row.before["name"] == "Doomed"
    assert row.after is None


async def test_model_set_api_key_is_audited_event_only(
    auth_db_client: AsyncClient, db_session: AsyncSession, model_admin_role: Role
) -> None:
    caller = await _caller(db_session, model_admin_role, email="model-setkey@example.com")
    model = await _a_model(db_session, name="KeyTarget")
    model_id = model.id  # capture before expire_all

    response = await auth_db_client.put(
        f"/api/v1/ai-models/{model_id}/api-key",
        json={"api_key": "sk-secret-SETKEY-999"},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    row = await _one_audit(db_session, AuditAction.AI_MODEL_CREDENTIAL_SET)
    assert row.object_type == "ai_model"
    assert row.object_id == model_id
    assert row.before is None  # event-only — never the key
    assert row.after is None
    assert "sk-secret-SETKEY-999" not in (str(row.before) + str(row.after) + str(row.context))


async def test_model_clear_api_key_is_audited_event_only(
    auth_db_client: AsyncClient, db_session: AsyncSession, model_admin_role: Role
) -> None:
    caller = await _caller(db_session, model_admin_role, email="model-clearkey@example.com")
    model = await _a_model(db_session, name="KeyClear", api_key_encrypted="CIPHER-abc")
    model_id = model.id  # capture before expire_all

    response = await auth_db_client.delete(f"/api/v1/ai-models/{model_id}/api-key", headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_204_NO_CONTENT
    row = await _one_audit(db_session, AuditAction.AI_MODEL_CREDENTIAL_CLEAR)
    assert row.object_id == model_id
    assert row.before is None
    assert row.after is None


async def test_bulk_model_create_is_audited_per_row(
    auth_db_client: AsyncClient, db_session: AsyncSession, model_admin_role: Role
) -> None:
    caller = await _caller(db_session, model_admin_role, email="bulk-model-create@example.com")

    response = await auth_db_client.post(
        "/api/v1/ai-models/bulk",
        json={
            "rows": [
                {
                    "row_key": "a",
                    "data": {
                        "name": "A",
                        "model_alias": "a-x",
                        "provider": "openai",
                        "provider_model_id": "a-id",
                    },
                },
                {
                    "row_key": "b",
                    "data": {
                        "name": "B",
                        "model_alias": "b-x",
                        "provider": "openai",
                        "provider_model_id": "b-id",
                    },
                },
            ]
        },
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    db_session.expire_all()
    count = (
        await db_session.execute(
            select(func.count()).select_from(AuditLog).where(col(AuditLog.action) == AuditAction.AI_MODEL_CREATE.value)
        )
    ).scalar_one()
    assert count == 2


async def test_bulk_set_api_keys_is_audited_per_row_event_only(
    auth_db_client: AsyncClient, db_session: AsyncSession, model_admin_role: Role
) -> None:
    caller = await _caller(db_session, model_admin_role, email="bulk-setkeys@example.com")
    await _a_model(db_session, name="bk1")
    await _a_model(db_session, name="bk2")

    response = await auth_db_client.post(
        "/api/v1/ai-models/api-keys/bulk",
        json={
            "rows": [
                {"row_key": "a", "data": {"name": "bk1", "api_key": "sk-BULKKEY-1"}},
                {"row_key": "b", "data": {"name": "bk2", "api_key": "sk-BULKKEY-2"}},
            ]
        },
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    db_session.expire_all()
    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(col(AuditLog.action) == AuditAction.AI_MODEL_CREDENTIAL_SET.value)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    for r in rows:
        assert r.before is None
        assert r.after is None
        assert "sk-BULKKEY" not in (str(r.before) + str(r.after) + str(r.context))


# reviews: assign / verdict / unassign


@pytest_asyncio.fixture
async def review_admin_role(db_session: AsyncSession) -> Role:
    """Reviews CRUD + the `evaluation_groups:manage` break-glass (so the caller may assign, record,
    and unassign any review in a visible group) + audit read."""
    role = Role(
        name="review-admin",
        description="reviews crud + manage + audit",
        permissions=[
            Permission.REVIEWS_READ.value,
            Permission.REVIEWS_CREATE.value,
            Permission.REVIEWS_UPDATE.value,
            Permission.REVIEWS_DELETE.value,
            Permission.EVALUATION_GROUPS_MANAGE.value,
            Permission.AUDIT_READ.value,
        ],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


async def _a_flag(db_session: AsyncSession, *, author_id: UUID) -> MessageFlag:
    """A live public group→evaluation→conversation graph plus a flag on it, authored by ``author_id``."""
    group = await persist_evaluation_group(db_session, created_by_id=author_id)
    evaluation = Evaluation(title="Eval", description="d", evaluation_group_id=group.id, created_by_id=author_id)
    db_session.add(evaluation)
    await db_session.flush()
    alias = f"m-{uuid4().hex[:8]}"
    model = AiModel(name=alias, model_alias=alias, provider=ProviderVendor.ANTHROPIC, provider_model_id=f"{alias}-id")
    db_session.add(model)
    await db_session.flush()
    scenario = Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0)
    db_session.add(scenario)
    await db_session.flush()
    assignment = EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id)
    conv_group = ConversationGroup(user_id=author_id, evaluation_id=evaluation.id, name="g", scenario_id=scenario.id)
    db_session.add(assignment)
    db_session.add(conv_group)
    await db_session.flush()
    conversation = Conversation(
        user_id=author_id,
        evaluation_id=evaluation.id,
        evaluation_ai_model_id=assignment.id,
        conversation_group_id=conv_group.id,
        scenario_id=scenario.id,
    )
    db_session.add(conversation)
    await db_session.flush()
    flag = MessageFlag(
        reason="exploit-worthy",
        created_by_id=author_id,
        conversation_id=conversation.id,
        evaluation_id=evaluation.id,
        evaluation_group_id=group.id,
    )
    db_session.add(flag)
    await db_session.flush()
    await db_session.refresh(flag)
    return flag


@pytest.mark.unit  # pure function — belongs in the unit suite, not the DB/integration run
def test_review_snapshot_curated_fields() -> None:
    review = Review(
        message_flag_id=uuid4(),
        reviewer_id=uuid4(),
        assigned_by_id=uuid4(),
        evaluation_id=uuid4(),
        status=ReviewStatus.APPROVED,
        successful_exploit=True,
        unique_exploit=False,
        valid_submission=True,
        number_prompts=3,
        notes="n",
    )
    snapshot = _review_snapshot(review)
    assert set(snapshot) == {
        "reviewer_id",
        "message_flag_id",
        "status",
        "successful_exploit",
        "unique_exploit",
        "valid_submission",
        "number_prompts",
        "notes",
    }
    assert snapshot["status"] == "approved"
    assert snapshot["reviewer_id"] == str(review.reviewer_id)


async def test_review_assign_is_audited(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    review_admin_role: Role,
    system_roles: dict[str, Role],
    celery_enqueue_stub: MagicMock,
) -> None:
    caller = await _caller(db_session, review_admin_role, email="rv-assign@example.com")
    reviewer = await _caller(db_session, system_roles["annotator"], email="rv-reviewer@example.com")
    reviewer_id = reviewer.id  # capture before expire_all
    flag = await _a_flag(db_session, author_id=caller.id)
    flag_id = flag.id

    response = await auth_db_client.post(
        "/api/v1/reviews",
        json={"message_flag_id": str(flag_id), "reviewer_id": str(reviewer_id)},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_201_CREATED
    row = await _one_audit(db_session, AuditAction.REVIEW_ASSIGN)
    assert row.object_type == "review"
    assert row.object_id == UUID(response.json()["id"])
    assert row.after is not None
    assert row.after["reviewer_id"] == str(reviewer_id)
    assert row.after["message_flag_id"] == str(flag_id)
    assert row.after["status"] == "pending"


async def test_review_verdict_is_audited_with_diff(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    review_admin_role: Role,
    system_roles: dict[str, Role],
    celery_enqueue_stub: MagicMock,
) -> None:
    caller = await _caller(db_session, review_admin_role, email="rv-verdict@example.com")
    reviewer = await _caller(db_session, system_roles["annotator"], email="rv-verdict-reviewer@example.com")
    flag = await _a_flag(db_session, author_id=caller.id)

    assign = await auth_db_client.post(
        "/api/v1/reviews",
        json={"message_flag_id": str(flag.id), "reviewer_id": str(reviewer.id)},
        headers=_auth(_token(caller)),
    )
    review_id = assign.json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/reviews/{review_id}",
        json={"successful_exploit": True, "valid_submission": True, "status": "approved"},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    row = await _one_audit(db_session, AuditAction.REVIEW_VERDICT)
    assert row.object_id == UUID(review_id)
    assert row.before is not None
    assert row.before["status"] == "pending"
    assert row.after is not None
    assert row.after["status"] == "approved"
    assert row.after["successful_exploit"] is True


async def test_review_unassign_is_audited_before_only(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    review_admin_role: Role,
    system_roles: dict[str, Role],
    celery_enqueue_stub: MagicMock,
) -> None:
    caller = await _caller(db_session, review_admin_role, email="rv-unassign@example.com")
    reviewer = await _caller(db_session, system_roles["annotator"], email="rv-unassign-reviewer@example.com")
    reviewer_id = reviewer.id  # capture before expire_all
    flag = await _a_flag(db_session, author_id=caller.id)

    assign = await auth_db_client.post(
        "/api/v1/reviews",
        json={"message_flag_id": str(flag.id), "reviewer_id": str(reviewer_id)},
        headers=_auth(_token(caller)),
    )
    review_id = assign.json()["id"]

    response = await auth_db_client.delete(f"/api/v1/reviews/{review_id}", headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_204_NO_CONTENT
    row = await _one_audit(db_session, AuditAction.REVIEW_UNASSIGN)
    assert row.object_id == UUID(review_id)
    assert row.before is not None
    assert row.before["reviewer_id"] == str(reviewer_id)
    assert row.after is None


# message flags: create / update / delete


@pytest_asyncio.fixture
async def flag_admin_role(db_session: AsyncSession) -> Role:
    """Full flag CRUD + audit read. Authoring is owner-only, so the caller owns its own flags."""
    role = Role(
        name="flag-admin",
        description="flags crud + audit",
        permissions=[
            Permission.FLAGS_READ.value,
            Permission.FLAGS_CREATE.value,
            Permission.FLAGS_UPDATE.value,
            Permission.FLAGS_DELETE.value,
            Permission.AUDIT_READ.value,
        ],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


async def _owned_conversation_message(db_session: AsyncSession, owner_id: UUID) -> tuple[UUID, UUID]:
    """A caller-owned conversation with one live assistant message → ``(conversation_id, message_id)``."""
    group = await persist_evaluation_group(db_session, created_by_id=owner_id)
    evaluation = Evaluation(title="Eval", description="d", evaluation_group_id=group.id, created_by_id=owner_id)
    db_session.add(evaluation)
    await db_session.flush()
    alias = f"m-{uuid4().hex[:8]}"
    model = AiModel(name=alias, model_alias=alias, provider=ProviderVendor.ANTHROPIC, provider_model_id=f"{alias}-id")
    db_session.add(model)
    await db_session.flush()
    scenario = Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0)
    db_session.add(scenario)
    await db_session.flush()
    assignment = EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id)
    conv_group = ConversationGroup(user_id=owner_id, evaluation_id=evaluation.id, name="g", scenario_id=scenario.id)
    db_session.add(assignment)
    db_session.add(conv_group)
    await db_session.flush()
    conversation = Conversation(
        user_id=owner_id,
        evaluation_id=evaluation.id,
        evaluation_ai_model_id=assignment.id,
        conversation_group_id=conv_group.id,
        scenario_id=scenario.id,
    )
    db_session.add(conversation)
    await db_session.flush()
    turn = Turn(conversation_id=conversation.id, turn_index=0)
    db_session.add(turn)
    await db_session.flush()
    message = ConvMessage(turn_id=turn.id, role=MessageRole.ASSISTANT, status=MessageStatus.COMPLETE, content="hi")
    db_session.add(message)
    await db_session.flush()
    await db_session.refresh(conversation)
    await db_session.refresh(message)
    return conversation.id, message.id


@pytest.mark.unit  # pure function — belongs in the unit suite, not the DB/integration run
def test_flag_snapshot_curated_fields() -> None:
    flag = MessageFlag(
        reason="r",
        red_flagged=True,
        comment="c",
        created_by_id=uuid4(),
        conversation_id=uuid4(),
        evaluation_id=uuid4(),
        evaluation_group_id=uuid4(),
        status=FlagStatus.PENDING,
    )
    snapshot = _flag_snapshot(flag)
    assert set(snapshot) == {"reason", "red_flagged", "comment", "status"}
    assert snapshot["status"] == "pending"
    assert snapshot["red_flagged"] is True


@pytest_asyncio.fixture
async def conversation_updater_role(db_session: AsyncSession) -> Role:
    """Conversation read/update — the pair the tag write rides on."""
    role = Role(
        name="conv-tag-updater",
        description="conversations read/update",
        permissions=[Permission.CONVERSATIONS_READ.value, Permission.CONVERSATIONS_UPDATE.value],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


async def test_conversation_tag_write_is_audited_with_diff(
    auth_db_client: AsyncClient, db_session: AsyncSession, conversation_updater_role: Role
) -> None:
    # Tags are prompt context the model acts on, so the change is audited with both sides —
    # `before={} after={}` would make the row useless for answering "who set that context".
    caller = await _caller(db_session, conversation_updater_role, email="conv-tags@example.com")
    caller_id = caller.id  # capture before expire_all
    conversation_id, _ = await _owned_conversation_message(db_session, caller.id)
    conversation = await db_session.get(Conversation, conversation_id)
    assert conversation is not None
    conversation.tags = {"env": "dev"}
    db_session.add(conversation)
    await db_session.flush()
    evaluation_id = conversation.evaluation_id

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation_id}/conversations/{conversation_id}",
        json={"tags": {"env": "prod"}},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_200_OK
    row = await _one_audit(db_session, AuditAction.CONVERSATION_TAGS_UPDATE)
    assert row.actor_id == caller_id
    assert row.object_type == "conversation"
    assert row.object_id == conversation_id
    assert row.before == {"tags": {"env": "dev"}}
    assert row.after == {"tags": {"env": "prod"}}
    assert row.context["evaluation_id"] == str(evaluation_id)


async def test_conversation_patch_without_tag_change_writes_no_audit_row(
    auth_db_client: AsyncClient, db_session: AsyncSession, conversation_updater_role: Role
) -> None:
    # The console PATCHes the whole map, so re-sending the same tags (or renaming with tags
    # untouched) must not append a no-op row.
    caller = await _caller(db_session, conversation_updater_role, email="conv-tags-noop@example.com")
    conversation_id, _ = await _owned_conversation_message(db_session, caller.id)
    conversation = await db_session.get(Conversation, conversation_id)
    assert conversation is not None
    conversation.tags = {"env": "dev"}
    db_session.add(conversation)
    await db_session.flush()
    evaluation_id = conversation.evaluation_id

    unchanged = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation_id}/conversations/{conversation_id}",
        json={"tags": {"env": "dev"}},
        headers=_auth(_token(caller)),
    )
    renamed = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation_id}/conversations/{conversation_id}",
        json={"title": "renamed"},
        headers=_auth(_token(caller)),
    )

    assert unchanged.status_code == status.HTTP_200_OK
    assert renamed.status_code == status.HTTP_200_OK
    db_session.expire_all()
    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(col(AuditLog.action) == AuditAction.CONVERSATION_TAGS_UPDATE.value)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


async def test_flag_create_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, flag_admin_role: Role
) -> None:
    caller = await _caller(db_session, flag_admin_role, email="flag-create@example.com")
    caller_id = caller.id  # capture before expire_all
    conversation_id, message_id = await _owned_conversation_message(db_session, caller.id)

    response = await auth_db_client.post(
        "/api/v1/message-flags",
        json={
            "conversation_id": str(conversation_id),
            "message_ids": [str(message_id)],
            "reason": "exploit",
            "red_flagged": True,
        },
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_201_CREATED
    row = await _one_audit(db_session, AuditAction.FLAG_CREATE)
    assert row.actor_id == caller_id
    assert row.object_type == "message_flag"
    assert row.object_id == UUID(response.json()["id"])
    assert row.after is not None
    assert row.after["reason"] == "exploit"
    assert row.after["red_flagged"] is True
    assert row.after["status"] == "pending"


async def test_flag_update_is_audited_with_diff(
    auth_db_client: AsyncClient, db_session: AsyncSession, flag_admin_role: Role
) -> None:
    caller = await _caller(db_session, flag_admin_role, email="flag-update@example.com")
    flag = await _a_flag(db_session, author_id=caller.id)  # reason == "exploit-worthy"
    flag_id = flag.id  # capture before expire_all

    response = await auth_db_client.patch(
        f"/api/v1/message-flags/{flag_id}", json={"reason": "revised"}, headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_200_OK
    row = await _one_audit(db_session, AuditAction.FLAG_UPDATE)
    assert row.object_id == flag_id
    assert row.before == {"reason": "exploit-worthy"}
    assert row.after == {"reason": "revised"}


async def test_flag_delete_is_audited_before_only(
    auth_db_client: AsyncClient, db_session: AsyncSession, flag_admin_role: Role
) -> None:
    caller = await _caller(db_session, flag_admin_role, email="flag-delete@example.com")
    flag = await _a_flag(db_session, author_id=caller.id)
    flag_id = flag.id  # capture before expire_all

    response = await auth_db_client.delete(f"/api/v1/message-flags/{flag_id}", headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_204_NO_CONTENT
    row = await _one_audit(db_session, AuditAction.FLAG_DELETE)
    assert row.object_id == flag_id
    assert row.before is not None
    assert row.before["reason"] == "exploit-worthy"
    assert row.after is None


# notes: create (after) / update (diff, and nothing at all when nothing changed) / delete (before-only)


@pytest_asyncio.fixture
async def note_admin_role(db_session: AsyncSession) -> Role:
    """Full note CRUD + audit read. Authoring needs no ownership of the conversation."""
    role = Role(
        name="note-admin",
        description="notes crud + audit",
        permissions=[
            Permission.NOTES_READ.value,
            Permission.NOTES_CREATE.value,
            Permission.NOTES_UPDATE.value,
            Permission.NOTES_DELETE.value,
            Permission.AUDIT_READ.value,
        ],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest.mark.unit  # pure function — belongs in the unit suite, not the DB/integration run
def test_note_snapshot_curated_fields() -> None:
    # Curated to the one editable field; pins the keyset so a later column can't leak in silently.
    note = Note(
        text="Complies after the reframing.",
        created_by_id=uuid4(),
        conversation_id=uuid4(),
        evaluation_id=uuid4(),
        evaluation_group_id=uuid4(),
    )
    assert _note_snapshot(note) == {"text": "Complies after the reframing."}


async def test_note_create_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, note_admin_role: Role
) -> None:
    caller = await _caller(db_session, note_admin_role, email="note-create@example.com")
    caller_id = caller.id  # capture before expire_all
    headers = _auth(_token(caller))
    target = await persist_conversation_target(db_session)
    expected_context = {  # capture before expire_all
        "conversation_id": str(target.conversation.id),
        "evaluation_id": str(target.evaluation.id),
        "evaluation_group_id": str(target.group.id),
    }

    response = await auth_db_client.post(
        "/api/v1/notes",
        json={
            "conversation_id": str(target.conversation.id),
            "message_ids": [str(target.messages[0].id)],
            "text": "Complies after the reframing.",
        },
        headers=headers,
    )

    assert response.status_code == status.HTTP_201_CREATED
    row = await _one_audit(db_session, AuditAction.NOTE_CREATE)
    assert row.actor_id == caller_id
    assert row.actor_email == "note-create@example.com"
    assert row.object_type == "note"
    assert row.object_id == UUID(response.json()["id"])
    assert row.before is None
    assert row.after == {"text": "Complies after the reframing.", "message_count": 1}
    # Authoring is not ownership-scoped, so the trail must say which transcript was
    # noted — the note row itself is unreachable once an ancestor dies.
    assert row.context == expected_context


async def test_note_update_is_audited_with_diff(
    auth_db_client: AsyncClient, db_session: AsyncSession, note_admin_role: Role
) -> None:
    caller = await _caller(db_session, note_admin_role, email="note-update@example.com")
    headers = _auth(_token(caller))
    target = await persist_conversation_target(db_session)
    conversation_id = str(target.conversation.id)  # capture before expire_all
    created = (
        await auth_db_client.post(
            "/api/v1/notes",
            json={
                "conversation_id": str(target.conversation.id),
                "message_ids": [str(target.messages[0].id)],
                "text": "first reading",
            },
            headers=headers,
        )
    ).json()

    response = await auth_db_client.patch(
        f"/api/v1/notes/{created['id']}", json={"text": "revised reading"}, headers=headers
    )

    assert response.status_code == status.HTTP_200_OK
    row = await _one_audit(db_session, AuditAction.NOTE_UPDATE)
    assert row.object_id == UUID(created["id"])
    assert row.actor_email == "note-update@example.com"
    assert row.before == {"text": "first reading"}
    assert row.after == {"text": "revised reading"}
    assert row.context["conversation_id"] == conversation_id


@pytest.mark.parametrize("payload", [{}, {"text": "untouched"}], ids=["empty_body", "same_value"])
async def test_note_update_without_changes_records_nothing(
    auth_db_client: AsyncClient, db_session: AsyncSession, note_admin_role: Role, payload: dict[str, str]
) -> None:
    """An empty PATCH succeeds and writes no audit row — the trail records changes, not requests.

    Re-sending the *same* value is the case the `changed_fields` guard exists for: an
    empty body already writes nothing because `model_fields_set` is empty.
    """
    caller = await _caller(db_session, note_admin_role, email="note-noop@example.com")
    headers = _auth(_token(caller))
    target = await persist_conversation_target(db_session)
    created = (
        await auth_db_client.post(
            "/api/v1/notes",
            json={
                "conversation_id": str(target.conversation.id),
                "message_ids": [str(target.messages[0].id)],
                "text": "untouched",
            },
            headers=headers,
        )
    ).json()

    response = await auth_db_client.patch(f"/api/v1/notes/{created['id']}", json=payload, headers=headers)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["text"] == "untouched"
    db_session.expire_all()
    update_rows = (
        (await db_session.execute(select(AuditLog).where(col(AuditLog.action) == AuditAction.NOTE_UPDATE.value)))
        .scalars()
        .all()
    )
    assert list(update_rows) == []


async def test_note_delete_is_audited_before_only(
    auth_db_client: AsyncClient, db_session: AsyncSession, note_admin_role: Role
) -> None:
    caller = await _caller(db_session, note_admin_role, email="note-delete@example.com")
    headers = _auth(_token(caller))
    target = await persist_conversation_target(db_session)
    conversation_id = str(target.conversation.id)  # capture before expire_all
    created = (
        await auth_db_client.post(
            "/api/v1/notes",
            json={
                "conversation_id": str(target.conversation.id),
                "message_ids": [str(target.messages[0].id)],
                "text": "to be removed",
            },
            headers=headers,
        )
    ).json()

    response = await auth_db_client.delete(f"/api/v1/notes/{created['id']}", headers=headers)

    assert response.status_code == status.HTTP_204_NO_CONTENT
    row = await _one_audit(db_session, AuditAction.NOTE_DELETE)
    assert row.object_id == UUID(created["id"])
    assert row.actor_email == "note-delete@example.com"
    assert row.before == {"text": "to be removed"}
    assert row.after is None
    assert row.context["conversation_id"] == conversation_id


# exports: create (before the manual commit, only if freshly created) / delete (before-only)


@pytest_asyncio.fixture
async def export_role(db_session: AsyncSession) -> Role:
    """Read access to the exported data; export create/delete authorize on target ownership."""
    role = Role(name="exporter", description="exports", permissions=[Permission.FLAGS_READ.value])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


async def test_export_create_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, export_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Neutralize the Celery enqueue (dispatched after commit) so the route doesn't reach a broker.
    monkeypatch.setattr("app.api.v1.exports.run_export_job", SimpleNamespace(delay=lambda *a, **k: None))
    caller = await _caller(db_session, export_role, email="exp-create@example.com")
    caller_id = caller.id  # capture before expire_all
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)
    evaluation = Evaluation(title="Eval", description="d", evaluation_group_id=group.id, created_by_id=caller.id)
    db_session.add(evaluation)
    await db_session.flush()
    eval_id = evaluation.id

    response = await auth_db_client.post(
        "/api/v1/exports/jobs",
        json={"template": "flags", "evaluation_id": str(eval_id)},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_202_ACCEPTED
    row = await _one_audit(db_session, AuditAction.EXPORT_CREATE)
    assert row.actor_id == caller_id
    assert row.object_type == "export_job"
    assert row.object_id == UUID(response.json()["id"])
    assert row.after is not None
    assert row.after["template"] == "flags"
    assert row.after["evaluation_id"] == str(eval_id)


async def test_export_create_enqueue_failure_drops_job_and_audit_row(
    auth_db_client: AsyncClient, db_session: AsyncSession, export_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Broker outage: the post-commit enqueue raises, so the route compensates by hard-deleting the
    # just-created job. The EXPORT_CREATE audit row committed with it has no FK cascade (object_id is
    # a bare UUID), so it must be dropped in the same compensation or it dangles past a deleted job.
    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("broker down")

    monkeypatch.setattr("app.api.v1.exports.run_export_job", SimpleNamespace(delay=_boom))
    caller = await _caller(db_session, export_role, email="exp-enqueue-fail@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)
    evaluation = Evaluation(title="Eval", description="d", evaluation_group_id=group.id, created_by_id=caller.id)
    db_session.add(evaluation)
    await db_session.flush()
    eval_id = evaluation.id

    response = await auth_db_client.post(
        "/api/v1/exports/jobs",
        json={"template": "flags", "evaluation_id": str(eval_id)},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    db_session.expire_all()
    assert (await db_session.execute(select(func.count()).select_from(ExportJob))).scalar_one() == 0
    audit_count = (
        await db_session.execute(
            select(func.count()).select_from(AuditLog).where(col(AuditLog.action) == AuditAction.EXPORT_CREATE.value)
        )
    ).scalar_one()
    assert audit_count == 0


async def test_export_delete_is_audited_before_only(
    auth_db_client: AsyncClient, db_session: AsyncSession, export_role: Role
) -> None:
    caller = await _caller(db_session, export_role, email="exp-delete@example.com")
    group = await persist_evaluation_group(db_session, created_by_id=caller.id)
    job = ExportJob(
        template="flags",
        requested_by_id=caller.id,
        evaluation_group_id=group.id,
        status=ExportJobStatus.FAILED,  # terminal → deletable, no stored file to remove
    )
    db_session.add(job)
    await db_session.flush()
    job_id = job.id  # capture before expire_all

    response = await auth_db_client.delete(f"/api/v1/exports/jobs/{job_id}", headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_204_NO_CONTENT
    row = await _one_audit(db_session, AuditAction.EXPORT_DELETE)
    assert row.object_id == job_id
    assert row.before is not None
    assert row.before["template"] == "flags"
    assert row.after is None


# Auth account-lifecycle events (Slice A): success only, subject = the affected account,
# written best-effort by the middleware (never in the auth handler — must not block auth).


async def test_email_verify_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, plain_role: Role, audit_via_test_session: None
) -> None:
    # Seed a PENDING account + a live verification token, then consume it via the real route.
    user = await create_user_service(
        db_session, email="verify-me@example.com", roles=[plain_role], status=UserStatus.PENDING
    )
    user_id = user.id  # capture before expire_all
    raw = generate_raw_token()
    db_session.add(
        EmailVerification(
            user_id=user_id, token_hash=hash_token(raw), expires_at=datetime.now(UTC) + timedelta(hours=1)
        )
    )
    await db_session.flush()

    response = await auth_db_client.post("/api/v1/auth/register/verify", json={"token": raw})

    assert response.status_code == status.HTTP_204_NO_CONTENT
    db_session.expire_all()
    row = (
        await db_session.execute(select(AuditLog).where(col(AuditLog.action) == AuditAction.AUTH_EMAIL_VERIFIED.value))
    ).scalar_one()
    assert row.object_type == "user"
    assert row.object_id == user_id
    assert row.actor_id is None
    assert row.actor_email is None
    assert row.before is None
    assert row.after is None
    # No email or raw token may appear anywhere in the row.
    haystack = str(row.actor_email) + str(row.context) + str(row.before) + str(row.after)
    assert "verify-me@example.com" not in haystack
    assert raw not in haystack


async def test_invitation_accept_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, plain_role: Role, audit_via_test_session: None
) -> None:
    # Seed an INVITED account + a live invitation token, then accept via the real route.
    user = await create_user_service(
        db_session, email="invited@example.com", roles=[plain_role], status=UserStatus.INVITED
    )
    user_id = user.id  # capture before expire_all
    raw = generate_raw_token()
    db_session.add(
        Invitation(user_id=user_id, token_hash=hash_token(raw), expires_at=datetime.now(UTC) + timedelta(hours=1))
    )
    await db_session.flush()

    response = await auth_db_client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": raw, "password": "SuperSecret-Accept-12345", "first_name": "Ada", "last_name": "Lovelace"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    db_session.expire_all()
    row = (
        await db_session.execute(select(AuditLog).where(col(AuditLog.action) == AuditAction.INVITATION_ACCEPT.value))
    ).scalar_one()
    assert row.object_type == "user"
    assert row.object_id == user_id
    assert row.actor_id is None
    assert row.actor_email is None
    assert row.before is None
    assert row.after is None
    haystack = str(row.actor_email) + str(row.context) + str(row.before) + str(row.after)
    assert "invited@example.com" not in haystack
    assert raw not in haystack


async def test_invitation_accept_bad_token_writes_no_audit_row(
    auth_db_client: AsyncClient, db_session: AsyncSession, audit_via_test_session: None
) -> None:
    # A non-2xx lifecycle outcome (unknown token → 4xx) leaves no audit row — the arm fires
    # only on _SUCCESS, and no subject is ever stashed.
    response = await auth_db_client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": "no-such-token-xyz", "password": "SuperSecret-Accept-12345"},
    )

    assert response.status_code in (status.HTTP_404_NOT_FOUND, status.HTTP_410_GONE)
    db_session.expire_all()
    count = (
        await db_session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(col(AuditLog.action) == AuditAction.INVITATION_ACCEPT.value)
        )
    ).scalar_one()
    assert count == 0


async def test_email_verify_bad_token_writes_no_audit_row(
    auth_db_client: AsyncClient, db_session: AsyncSession, audit_via_test_session: None
) -> None:
    # Symmetric with invitation-accept: a failed verify (unknown token → 4xx) stashes no subject
    # and is non-2xx, so no AUTH_EMAIL_VERIFIED row is written.
    response = await auth_db_client.post("/api/v1/auth/register/verify", json={"token": "no-such-token-xyz"})

    assert response.status_code >= status.HTTP_400_BAD_REQUEST
    db_session.expire_all()
    count = (
        await db_session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(col(AuditLog.action) == AuditAction.AUTH_EMAIL_VERIFIED.value)
        )
    ).scalar_one()
    assert count == 0


async def test_lifecycle_middleware_skips_non_success_even_with_subject(
    db_session: AsyncSession, audit_via_test_session: None
) -> None:
    # The 2xx gate stands on its own: a lifecycle route returning non-2xx writes NO row even when a
    # subject was already stashed. No real route stashes-then-fails, so drive the middleware directly
    # (mutation-kills removing `status_code in _SUCCESS` from the lifecycle arm).
    await _drive_access_middleware(
        method="POST",
        route_name="post_invitation_accept_endpoint",
        status_code=status.HTTP_400_BAD_REQUEST,
        audit_subject=uuid4(),
    )

    db_session.expire_all()
    count = (
        await db_session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(col(AuditLog.action) == AuditAction.INVITATION_ACCEPT.value)
        )
    ).scalar_one()
    assert count == 0


async def test_lifecycle_middleware_no_subject_writes_no_row(
    db_session: AsyncSession, audit_via_test_session: None
) -> None:
    # The subject-None guard: a 2xx lifecycle route that stashed NO subject writes no row (rather
    # than a subjectless object_id=None one) — mutation-kills removing the `if subject is None` return.
    await _drive_access_middleware(
        method="POST",
        route_name="post_invitation_accept_endpoint",
        status_code=status.HTTP_204_NO_CONTENT,
    )

    db_session.expire_all()
    count = (
        await db_session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(col(AuditLog.action) == AuditAction.INVITATION_ACCEPT.value)
        )
    ).scalar_one()
    assert count == 0


@pytest_asyncio.fixture
async def inviter_role(db_session: AsyncSession) -> Role:
    role = Role(name="auditor-inviter", description="invite users", permissions=[Permission.USERS_INVITE.value])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest.mark.usefixtures("celery_enqueue_stub")
async def test_invitation_resend_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, inviter_role: Role, system_roles: dict[str, Role]
) -> None:
    caller = await _caller(db_session, inviter_role, email="resender@example.com")
    target = await create_user_service(
        db_session,
        email="waiting@example.com",
        status=UserStatus.INVITED,
        roles=[system_roles["red_teamer"]],
    )
    target_id = target.id

    response = await auth_db_client.post(
        f"/api/v1/auth/users/{target_id}/invitation/resend", headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_200_OK
    db_session.expire_all()
    row = await _audit_row(db_session, AuditAction.INVITATION_RESEND)
    assert row.object_type == "user"
    assert row.object_id == target_id
    assert row.actor_email == "resender@example.com"


async def test_invitation_revoke_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, inviter_role: Role, system_roles: dict[str, Role]
) -> None:
    caller = await _caller(db_session, inviter_role, email="revoker@example.com")
    target = await create_user_service(
        db_session,
        email="waiting@example.com",
        status=UserStatus.INVITED,
        roles=[system_roles["red_teamer"]],
    )
    target_id = target.id
    db_session.add(
        Invitation(
            user_id=target_id,
            token_hash="9" * 64,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            status=InvitationStatus.PENDING,
        )
    )
    await db_session.flush()

    response = await auth_db_client.delete(f"/api/v1/auth/users/{target_id}/invitation", headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_204_NO_CONTENT
    db_session.expire_all()
    row = await _audit_row(db_session, AuditAction.INVITATION_REVOKE)
    assert row.object_type == "user"
    assert row.object_id == target_id


async def test_tag_key_add_is_audited_with_the_parent_evaluation_in_context(
    auth_db_client: AsyncClient, db_session: AsyncSession, mutator_role: Role
) -> None:
    # `object_id` is the tag-key row's own id, which no read endpoint surfaces and which removal
    # soft-deletes — so without `evaluation_id` in the context the row cannot be attributed to the
    # evaluation whose schema it changed.
    caller = await _caller(db_session, mutator_role, email="tagkey-add@example.com")
    evaluation = await _an_evaluation(db_session)
    evaluation_id, caller_id = evaluation.id, caller.id

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation_id}/tag-keys",
        json={"key": "env"},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_201_CREATED
    row = await _audit_row(db_session, AuditAction.EVALUATION_TAG_KEY_ADD)
    assert row.actor_id == caller_id
    assert row.object_type == "evaluation_tag_key"
    assert row.object_id == UUID(response.json()["id"])
    assert row.after == {"key": "env"}
    assert row.context["evaluation_id"] == str(evaluation_id)


async def test_tag_key_remove_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, mutator_role: Role
) -> None:
    caller = await _caller(db_session, mutator_role, email="tagkey-remove@example.com")
    evaluation = await _an_evaluation(db_session)
    evaluation_id = evaluation.id
    key_row = EvaluationTagKey(evaluation_id=evaluation_id, key="env")
    db_session.add(key_row)
    await db_session.flush()
    key_id = key_row.id

    response = await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation_id}/tag-keys/env", headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    row = await _audit_row(db_session, AuditAction.EVALUATION_TAG_KEY_REMOVE)
    assert row.object_type == "evaluation_tag_key"
    assert row.object_id == key_id
    assert row.before == {"key": "env"}
    assert row.context["evaluation_id"] == str(evaluation_id)


async def test_rejected_tag_key_add_writes_no_audit_row(
    auth_db_client: AsyncClient, db_session: AsyncSession, mutator_role: Role
) -> None:
    # A duplicate is a 409 and changes nothing, so the trail must not record a schema change.
    caller = await _caller(db_session, mutator_role, email="tagkey-dup@example.com")
    evaluation = await _an_evaluation(db_session)
    evaluation_id = evaluation.id
    db_session.add(EvaluationTagKey(evaluation_id=evaluation_id, key="env"))
    await db_session.flush()

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation_id}/tag-keys",
        json={"key": "env"},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    db_session.expire_all()
    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(col(AuditLog.action) == AuditAction.EVALUATION_TAG_KEY_ADD.value)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


# --------------------------------------------------------------------------------------
# Role management: the most privileged mutation surface
# --------------------------------------------------------------------------------------

# The caller is the `admin` system role, not a purpose-built one: `roles:manage` is
# non-delegable, so a custom role holding it is a state the API refuses to create.


async def test_role_create_is_audited(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    caller = await _caller(db_session, system_roles["admin"], email="role-create@example.com")

    response = await auth_db_client.post(
        "/api/v1/roles",
        json={"name": "audited_role", "display_name": "Audited", "permissions": [Permission.REVIEWS_READ.value]},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_201_CREATED
    row = await _audit_row(db_session, AuditAction.ROLE_CREATE)
    assert row.before is None
    assert row.after is not None
    assert row.after["name"] == "audited_role"
    assert row.after["permissions"] == [Permission.REVIEWS_READ.value]


async def test_role_deactivation_is_audited_with_diff(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The forensic case: deactivation force-logs out every holder, so who did it must be recorded.
    caller = await _caller(db_session, system_roles["admin"], email="role-update@example.com")
    role = await create_role_service(
        db_session, name="audited_toggle", display_name="Toggle", description=None, permissions=[]
    )
    role_id = role.id

    response = await auth_db_client.patch(
        f"/api/v1/roles/{role_id}", json={"is_active": False}, headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_200_OK
    row = await _audit_row(db_session, AuditAction.ROLE_UPDATE)
    assert row.object_id == role_id
    assert row.before == {"is_active": True}
    assert row.after == {"is_active": False}


async def test_role_delete_is_audited_with_the_full_snapshot(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    caller = await _caller(db_session, system_roles["admin"], email="role-delete@example.com")
    role = await create_role_service(
        db_session, name="audited_gone", display_name="Gone", description=None, permissions=[]
    )
    role_id = role.id

    response = await auth_db_client.delete(f"/api/v1/roles/{role_id}", headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_204_NO_CONTENT
    row = await _audit_row(db_session, AuditAction.ROLE_DELETE)
    assert row.object_id == role_id
    assert row.before is not None
    assert row.before["name"] == "audited_gone"
    assert row.after is None


async def test_noop_role_update_writes_no_audit_row(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Every field is optional, so an empty body is a valid PATCH that reaches no service at
    # all — the handler must skip the row rather than log empty before/after diffs.
    caller = await _caller(db_session, system_roles["admin"], email="role-noop@example.com")
    role = await create_role_service(
        db_session, name="audited_unchanged", display_name="Unchanged", description=None, permissions=[]
    )
    role_id = role.id

    response = await auth_db_client.patch(f"/api/v1/roles/{role_id}", json={}, headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_200_OK
    db_session.expire_all()
    count = (
        await db_session.execute(
            select(func.count()).select_from(AuditLog).where(col(AuditLog.action) == AuditAction.ROLE_UPDATE.value)
        )
    ).scalar_one()
    assert count == 0
