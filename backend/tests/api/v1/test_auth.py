"""Integration tests for the /api/v1/auth/* router."""

import json
from base64 import b64encode
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime
from urllib.parse import parse_qsl
from urllib.parse import urlsplit
from uuid import uuid4

import itsdangerous
import pytest
from authlib.integrations.base_client import OAuthError
from fastapi import status
from httpx import ASGITransport
from httpx import AsyncClient
from httpx import ConnectError
from httpx import Response
from joserfc.errors import ExpiredTokenError
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.audit.enums import AuditAction
from app.core.audit.models import AuditLog
from app.core.auth.models import ProviderIdentity
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.services import providers as providers_mod
from app.core.auth.services.jwt import decode_session_jwt
from app.core.auth.services.users import create_user as create_user_service
from app.core.config import Settings
from app.core.middleware import ScopedSessionMiddleware
from app.core.platform_settings.service import update_platform_settings
from app.main import app as fastapi_app
from tests.api.v1.conftest import PROBLEM_CT
from tests.api.v1.conftest import SECRET
from tests.api.v1.conftest import make_token


def _session_middleware_secret() -> str:
    """Return the `secret_key` actually bound to `ScopedSessionMiddleware`.

    The middleware's secret is captured at app-import time, before the
    `_configured_settings` fixture's `get_settings` monkey-patch lands, so
    forging a cookie against the fixture's settings value would fail
    signature verification. Pull the live value off `app.user_middleware`.
    """
    for mw in fastapi_app.user_middleware:
        if mw.cls is ScopedSessionMiddleware:
            return str(mw.kwargs["secret_key"])
    raise RuntimeError("ScopedSessionMiddleware is not wired into the app")


def _signed_oidc_state_cookie(payload: dict[str, str]) -> str:
    """Forge an `oidc_state` cookie that Starlette's `SessionMiddleware` will accept.

    Mirrors the b64+itsdangerous encoding in `starlette.middleware.sessions`
    so we can simulate the cookie that `/login` would leave behind without
    actually driving the full IdP roundtrip.
    """
    signer = itsdangerous.TimestampSigner(_session_middleware_secret())
    return signer.sign(b64encode(json.dumps(payload).encode())).decode()


@pytest.fixture
async def auth_client(_configured_settings: Settings) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.mark.integration
async def test_providers_lists_configured_oidc_names(auth_client: AsyncClient) -> None:
    response = await auth_client.get("/api/v1/auth/oidc/providers")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == ["google"]


@pytest.mark.integration
async def test_me_returns_live_roles_and_permission_union(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    role_a = Role(name="role-a", display_name="Role A", permissions=["flags:read", "flags:create"], is_system=False)
    role_b = Role(name="role-b", display_name="Role B", permissions=["flags:create", "reviews:read"], is_system=True)
    db_session.add_all([role_a, role_b])
    await db_session.flush()
    user = await create_user_service(db_session, email="rt@example.com", first_name="Ada", roles=[role_a, role_b])
    token = make_token(user)

    response = await auth_db_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    # Deduped, sorted union across both roles.
    assert body["permissions"] == ["flags:create", "flags:read", "reviews:read"]
    assert {r["name"] for r in body["roles"]} == {"role-a", "role-b"}
    assert {r["display_name"] for r in body["roles"]} == {"Role A", "Role B"}
    # Embedded roles are slim — identity + label only; effective perms live in the union above.
    assert all(set(r) == {"id", "name", "display_name"} for r in body["roles"])


@pytest.mark.integration
async def test_me_hides_inactive_roles(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    active = Role(name="me-active", display_name="Active", permissions=["flags:read"], is_system=False)
    later_inactive = Role(name="me-inactive", display_name="Inactive", permissions=["reviews:read"], is_system=False)
    db_session.add_all([active, later_inactive])
    await db_session.flush()
    user = await create_user_service(db_session, email="me-inactive@example.com", roles=[active, later_inactive])
    later_inactive.is_active = False
    db_session.add(later_inactive)
    await db_session.flush()
    token = make_token(user)

    response = await auth_db_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == status.HTTP_200_OK
    assert {r["name"] for r in response.json()["roles"]} == {"me-active"}


@pytest.mark.integration
async def test_me_session_fields_come_from_the_token(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    role = Role(name="rt", permissions=["flags:read"], is_system=False)
    db_session.add(role)
    await db_session.flush()
    user = await create_user_service(
        db_session, email="ada@example.com", first_name="Ada", email_verified=True, roles=[role]
    )

    response = await auth_db_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {make_token(user, provider='google')}"}
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["email"] == "ada@example.com"
    assert body["provider"] == "google"
    assert body["email_verified"] is True


@pytest.mark.integration
async def test_me_projects_names_and_password_state_from_the_db_row(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    # A rename after the token was minted must show up without a re-login;
    # the stale claims stay in the JWT, the response reads the row.
    role = Role(name="me-db", permissions=["flags:read"], is_system=False)
    db_session.add(role)
    await db_session.flush()
    user = await create_user_service(db_session, email="renamed@example.com", first_name="Ada", roles=[role])
    token = make_token(user)
    user.first_name = "Augusta"
    user.last_name = "King"
    db_session.add(user)
    await db_session.flush()

    response = await auth_db_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["first_name"] == "Augusta"
    assert body["last_name"] == "King"
    assert body["has_password"] is False

    user.password = "argon2-stand-in"
    db_session.add(user)
    await db_session.flush()

    response = await auth_db_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.json()["has_password"] is True


@pytest.mark.integration
async def test_me_falls_back_to_token_when_session_has_no_db_row(auth_db_client: AsyncClient) -> None:
    # A token whose subject has no live row (e.g. the account was removed mid-session):
    # it still carries flattened permissions, so /me reflects those with empty roles.
    user = User(id=uuid4(), email="oidc@example.com", email_verified_at=datetime.now(UTC), first_name="Odette")
    user.roles = [Role(name="ephemeral", permissions=["evaluations:read", "flags:read"], is_system=False)]
    token = make_token(user, provider="google")

    response = await auth_db_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["email"] == "oidc@example.com"
    assert body["provider"] == "google"
    assert body["first_name"] == "Odette"
    assert body["has_password"] is False
    assert body["roles"] == []
    assert body["permissions"] == ["evaluations:read", "flags:read"]


@pytest.fixture
async def oidc_default_role(db_session: AsyncSession) -> Role:
    """The `is_default` role a first OIDC login provisions its new account with."""
    role = Role(name="red_teamer", display_name="Red Teamer", permissions=["flags:read"], is_default=True)
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


def _callback_fragment(response: Response) -> dict[str, str]:
    """The redirect's fragment params — where the callback hands tokens to the SPA."""
    location = urlsplit(response.headers["location"])
    assert location.path == "/auth/callback", location
    return dict(parse_qsl(location.fragment))


@pytest.fixture
def audit_via_test_session(monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession) -> None:
    """Route `AuditAccessMiddleware`'s `standalone_session` to the test session.

    It writes in a fresh `standalone_session`, which needs the lifespan-managed engine
    (absent under `ASGITransport`) — see `tests/api/v1/test_audit_logs.py` for the
    original. No commit: a `flush` makes the row visible in this same session.
    """

    @asynccontextmanager
    async def _fake_standalone() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr("app.core.middleware.audit.standalone_session", _fake_standalone)


@pytest.mark.integration
@pytest.mark.usefixtures("oidc_default_role")
async def test_callback_redirects_to_the_spa_with_tokens_in_the_fragment(
    auth_db_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_userinfo = {
        "sub": "google-sub-xyz",
        "email": "ada@example.com",
        "email_verified": True,
        "given_name": "Ada",
        "family_name": "Lovelace",
    }

    async def fake_authorize_access_token(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"userinfo": fake_userinfo}

    client = providers_mod.get_provider("google")
    monkeypatch.setattr(client, "authorize_access_token", fake_authorize_access_token)

    response = await auth_db_client.get("/api/v1/auth/oidc/google/callback")

    assert response.status_code == status.HTTP_302_FOUND
    fragment = _callback_fragment(response)
    assert fragment["expires_in"] == "3600"
    assert fragment["refresh_token"]
    # Tokens must ride the fragment, never the query — a query string reaches the
    # FE server's access log and the next page's `Referer`.
    assert urlsplit(response.headers["location"]).query == ""
    assert response.headers["cache-control"] == "no-store"

    # Decode end-to-end to lock in `typ="access"` — otherwise a bug that minted
    # a refresh JWT in the access slot would slip past this happy-path test.
    session_user = decode_session_jwt(fragment["access_token"], secret=SECRET, algorithm="HS256")
    assert session_user.email == "ada@example.com"
    assert session_user.provider == "google"
    assert session_user.email_verified is True


@pytest.mark.integration
@pytest.mark.usefixtures("oidc_default_role", "audit_via_test_session")
async def test_callback_success_is_audited(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AuditAccessMiddleware` can't branch on status (every outcome the handler's own logic
    reaches is a 302), so the handler stashes the action itself — this proves that wiring
    end to end, not just the stash line in isolation.
    """

    async def fake_authorize_access_token(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"userinfo": {"sub": "google-sub-xyz", "email": "ada@example.com", "email_verified": True}}

    client = providers_mod.get_provider("google")
    monkeypatch.setattr(client, "authorize_access_token", fake_authorize_access_token)

    response = await auth_db_client.get("/api/v1/auth/oidc/google/callback")

    assert response.status_code == status.HTTP_302_FOUND
    session_user = decode_session_jwt(_callback_fragment(response)["access_token"], secret=SECRET, algorithm="HS256")
    db_session.expire_all()
    row = (
        await db_session.execute(select(AuditLog).where(col(AuditLog.action) == AuditAction.AUTH_LOGIN.value))
    ).scalar_one()
    assert row.object_type == "user"
    assert row.object_id == session_user.id
    assert row.actor_id is None
    assert row.actor_email is None


@pytest.mark.integration
@pytest.mark.usefixtures("oidc_default_role")
async def test_callback_clears_oidc_state_cookie_on_success(
    auth_db_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: `session.clear()` in the callback's `finally` must
    drop the short-lived `oidc_state` cookie left behind by `/login`.
    """

    async def fake_authorize_access_token(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "userinfo": {
                "sub": "google-sub-xyz",
                "email": "ada@example.com",
                "email_verified": True,
            },
        }

    client = providers_mod.get_provider("google")
    monkeypatch.setattr(client, "authorize_access_token", fake_authorize_access_token)

    # SessionMiddleware emits a clearing cookie (expires=1970) only when the
    # *inbound* request carried a validly signed session — otherwise the clear
    # is a no-op on an already-empty dict, and no Set-Cookie is sent. So we
    # seed the request with the same cookie `/login` would have stashed.
    cookie = _signed_oidc_state_cookie({"state": "abc", "nonce": "xyz"})

    response = await auth_db_client.get(
        "/api/v1/auth/oidc/google/callback",
        headers={"Cookie": f"oidc_state={cookie}"},
    )

    assert response.status_code == status.HTTP_302_FOUND
    set_cookies = response.headers.get_list("set-cookie")
    assert any("oidc_state=null" in c and "1970" in c for c in set_cookies), (
        f"oidc_state was not cleared. Set-Cookie headers: {set_cookies}"
    )


@pytest.mark.integration
async def test_callback_reports_missing_claims_on_the_fragment(
    auth_db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_authorize_access_token(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"userinfo": {"sub": "abc"}}  # email missing

    client = providers_mod.get_provider("google")
    monkeypatch.setattr(client, "authorize_access_token", fake_authorize_access_token)

    response = await auth_db_client.get("/api/v1/auth/oidc/google/callback")

    assert response.status_code == status.HTTP_302_FOUND
    assert _callback_fragment(response) == {"error": "invalid_claims"}


@pytest.mark.integration
@pytest.mark.usefixtures("audit_via_test_session")
async def test_callback_invalid_claims_writes_no_audit_row(
    auth_db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scoped deliberately: this never resolves an account, unlike `account_inactive` — same
    call as `_audit_login` skipping non-credential-outcome statuses (e.g. 422).
    """

    async def fake_authorize_access_token(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"userinfo": {"sub": "abc"}}  # email missing

    client = providers_mod.get_provider("google")
    monkeypatch.setattr(client, "authorize_access_token", fake_authorize_access_token)

    response = await auth_db_client.get("/api/v1/auth/oidc/google/callback")

    assert response.status_code == status.HTTP_302_FOUND
    db_session.expire_all()
    count = (
        await db_session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(col(AuditLog.action).in_([AuditAction.AUTH_LOGIN.value, AuditAction.AUTH_LOGIN_FAILED.value]))
        )
    ).scalar_one()
    assert count == 0


@pytest.mark.integration
async def test_callback_reports_invite_only_on_the_fragment(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Deliberately no `oidc_default_role`: the gate must refuse before any provisioning machinery runs.
    await update_platform_settings(db_session, invite_only=True)

    async def fake_authorize_access_token(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"userinfo": {"sub": "google-sub-999", "email": "new@example.com", "email_verified": True}}

    client = providers_mod.get_provider("google")
    monkeypatch.setattr(client, "authorize_access_token", fake_authorize_access_token)

    response = await auth_db_client.get("/api/v1/auth/oidc/google/callback")

    assert response.status_code == status.HTTP_302_FOUND
    assert _callback_fragment(response) == {"error": "invite_only"}


@pytest.mark.integration
async def test_callback_reports_a_deactivated_account_without_provisioning_it(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role = Role(name="deactivated-role", permissions=[], is_system=False)
    db_session.add(role)
    await db_session.flush()
    await create_user_service(db_session, email="off@example.com", roles=[role], status=UserStatus.INACTIVE)

    async def fake_authorize_access_token(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"userinfo": {"sub": "google-sub-off", "email": "off@example.com", "email_verified": True}}

    client = providers_mod.get_provider("google")
    monkeypatch.setattr(client, "authorize_access_token", fake_authorize_access_token)

    response = await auth_db_client.get("/api/v1/auth/oidc/google/callback")

    assert response.status_code == status.HTTP_302_FOUND
    # Opaque code — the SPA must not be able to tell "switched off" from "no account".
    assert _callback_fragment(response) == {"error": "account_inactive"}
    identities = await db_session.execute(
        ProviderIdentity.live_select().where(col(ProviderIdentity.subject) == "google-sub-off")
    )
    assert identities.scalars().all() == []


@pytest.mark.integration
@pytest.mark.usefixtures("audit_via_test_session")
async def test_callback_refused_login_is_audited_with_the_targeted_account(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The detection signal this exists for: which account a refused activation targeted."""
    role = Role(name="deactivated-role-2", permissions=[], is_system=False)
    db_session.add(role)
    await db_session.flush()
    off_user = await create_user_service(db_session, email="off2@example.com", roles=[role], status=UserStatus.INACTIVE)
    off_user_id = off_user.id

    async def fake_authorize_access_token(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"userinfo": {"sub": "google-sub-off-2", "email": "off2@example.com", "email_verified": True}}

    client = providers_mod.get_provider("google")
    monkeypatch.setattr(client, "authorize_access_token", fake_authorize_access_token)

    response = await auth_db_client.get("/api/v1/auth/oidc/google/callback")

    assert response.status_code == status.HTTP_302_FOUND
    db_session.expire_all()
    row = (
        await db_session.execute(select(AuditLog).where(col(AuditLog.action) == AuditAction.AUTH_LOGIN_FAILED.value))
    ).scalar_one()
    assert row.object_type == "user"
    assert row.object_id == off_user_id


@pytest.mark.integration
async def test_callback_unknown_provider_returns_404(auth_db_client: AsyncClient) -> None:
    response = await auth_db_client.get("/api/v1/auth/oidc/azure/callback")

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.headers["content-type"] == PROBLEM_CT
    body = response.json()
    assert body["title"] == "Not Found"
    assert body["status"] == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_callback_reports_an_unverified_email_on_the_fragment(
    auth_db_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_authorize_access_token(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "userinfo": {
                "sub": "google-sub-xyz",
                "email": "spoofed@example.com",
                "email_verified": False,
                "name": "Mallory",
            },
        }

    client = providers_mod.get_provider("google")
    monkeypatch.setattr(client, "authorize_access_token", fake_authorize_access_token)

    response = await auth_db_client.get("/api/v1/auth/oidc/google/callback")

    assert response.status_code == status.HTTP_302_FOUND
    assert _callback_fragment(response) == {"error": "email_unverified"}
    # No session leaked past the takeover guard.
    assert "access_token" not in response.headers["location"]


@pytest.mark.integration
@pytest.mark.parametrize(
    "oauth_error_code",
    ["access_denied", "login_required", "mismatching_state", "csrf_error", "server_error", "something_unmapped"],
)
async def test_callback_passes_oauth_error_codes_through_to_the_spa(
    auth_db_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    oauth_error_code: str,
) -> None:
    async def fake_authorize_access_token(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise OAuthError(error=oauth_error_code, description="simulated")

    client = providers_mod.get_provider("google")
    monkeypatch.setattr(client, "authorize_access_token", fake_authorize_access_token)

    response = await auth_db_client.get("/api/v1/auth/oidc/google/callback")

    assert response.status_code == status.HTTP_302_FOUND
    assert _callback_fragment(response) == {"error": oauth_error_code}


@pytest.mark.integration
async def test_callback_redirects_instead_of_500_on_a_network_failure_to_the_idp(
    auth_db_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """authlib's async OAuth2 client subclasses `httpx.AsyncClient` directly, so a
    connect timeout talking to Google's token endpoint raises httpx's own exception,
    not an `OAuthError` — this must not escape as a raw 500 mid-handshake.
    """

    async def fake_authorize_access_token(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise ConnectError("simulated connect failure")

    client = providers_mod.get_provider("google")
    monkeypatch.setattr(client, "authorize_access_token", fake_authorize_access_token)

    response = await auth_db_client.get("/api/v1/auth/oidc/google/callback")

    assert response.status_code == status.HTTP_302_FOUND
    assert _callback_fragment(response) == {"error": "oauth_error"}


@pytest.mark.integration
async def test_login_redirects_instead_of_500_on_a_network_failure_to_the_idp(
    auth_db_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`authorize_redirect` resolves `server_metadata_url` lazily on first use — a real
    outbound GET to Google's discovery endpoint. A connect failure there raises httpx's
    own exception on the entry leg, same as on the callback leg above, and must not
    strand a top-level browser navigation on a raw JSON 500 either.
    """

    async def fake_authorize_redirect(*_args: object, **_kwargs: object) -> None:
        raise ConnectError("simulated connect failure")

    client = providers_mod.get_provider("google")
    monkeypatch.setattr(client, "authorize_redirect", fake_authorize_redirect)

    response = await auth_db_client.get("/api/v1/auth/oidc/google/login")

    assert response.status_code == status.HTTP_302_FOUND
    assert _callback_fragment(response) == {"error": "oauth_error"}


@pytest.mark.integration
async def test_login_redirects_instead_of_500_on_a_non_json_discovery_response(
    auth_db_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`raise_for_status()` only covers >= 500 — a captive portal, a TLS-intercepting
    proxy, or an IdP maintenance page answering the discovery GET with 200 and an HTML
    body sails past it, and `resp.json()` raises `JSONDecodeError`, not `HTTPError`.
    """

    async def fake_authorize_redirect(*_args: object, **_kwargs: object) -> None:
        raise json.JSONDecodeError("simulated non-JSON discovery response", "<html>", 0)

    client = providers_mod.get_provider("google")
    monkeypatch.setattr(client, "authorize_redirect", fake_authorize_redirect)

    response = await auth_db_client.get("/api/v1/auth/oidc/google/login")

    assert response.status_code == status.HTTP_302_FOUND
    assert _callback_fragment(response) == {"error": "oauth_error"}


@pytest.mark.integration
async def test_login_redirects_instead_of_500_on_a_discovery_document_missing_the_authorize_endpoint(
    auth_db_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """authlib's `create_authorization_url` raises a bare `RuntimeError` when the
    discovery document has no `authorization_endpoint` — a broken IdP, not a bug here.
    """

    async def fake_authorize_redirect(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError('Missing "authorize_url" value')

    client = providers_mod.get_provider("google")
    monkeypatch.setattr(client, "authorize_redirect", fake_authorize_redirect)

    response = await auth_db_client.get("/api/v1/auth/oidc/google/login")

    assert response.status_code == status.HTTP_302_FOUND
    assert _callback_fragment(response) == {"error": "oauth_error"}


@pytest.mark.integration
async def test_callback_redirects_instead_of_500_on_a_non_json_token_response(
    auth_db_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same blind spot as the login leg, on `parse_response_token`'s `resp.json()` —
    a malformed token-endpoint response isn't an `HTTPError` either.
    """

    async def fake_authorize_access_token(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise json.JSONDecodeError("simulated non-JSON token response", "<html>", 0)

    client = providers_mod.get_provider("google")
    monkeypatch.setattr(client, "authorize_access_token", fake_authorize_access_token)

    response = await auth_db_client.get("/api/v1/auth/oidc/google/callback")

    assert response.status_code == status.HTTP_302_FOUND
    assert _callback_fragment(response) == {"error": "oauth_error"}


@pytest.mark.integration
async def test_callback_redirects_instead_of_500_on_an_invalid_id_token(
    auth_db_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`authorize_access_token` parses and validates the id_token inline via `joserfc` —
    a forged signature, an expired `exp`, or a nonce mismatch raises `joserfc`'s own
    exception, a third hierarchy unrelated to both `OAuthError` and `httpx.HTTPError`.
    """

    async def fake_authorize_access_token(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise ExpiredTokenError("exp")

    client = providers_mod.get_provider("google")
    monkeypatch.setattr(client, "authorize_access_token", fake_authorize_access_token)

    response = await auth_db_client.get("/api/v1/auth/oidc/google/callback")

    assert response.status_code == status.HTTP_302_FOUND
    assert _callback_fragment(response) == {"error": "invalid_claims"}
