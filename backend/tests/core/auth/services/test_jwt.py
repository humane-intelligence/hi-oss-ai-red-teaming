"""Session JWT — sign/verify roundtrip, expiry, tampering."""

from datetime import UTC
from datetime import datetime
from uuid import uuid4

import pytest
from joserfc import jwt as joserfc_jwt
from joserfc.jwk import OctKey

from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.services.jwt import InvalidSessionTokenError
from app.core.auth.services.jwt import decode_refresh_jwt
from app.core.auth.services.jwt import decode_session_jwt
from app.core.auth.services.jwt import encode_refresh_jwt
from app.core.auth.services.jwt import encode_session_jwt

SECRET = "test-secret-that-is-comfortably-long-enough-for-hs256"
ALG = "HS256"


def _user() -> User:
    now = datetime.now(UTC)
    return User(
        id=uuid4(),
        email="ada@example.com",
        email_verified_at=now,
        first_name="Ada",
        last_name="Lovelace",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.unit
def test_encode_decode_roundtrip_preserves_identity() -> None:
    user = _user()

    token, expires_in = encode_session_jwt(user, provider="google", secret=SECRET, algorithm=ALG, ttl_seconds=3600)
    session = decode_session_jwt(token, secret=SECRET, algorithm=ALG)

    assert expires_in == 3600
    assert session.id == user.id
    assert session.provider == "google"
    assert session.email == "ada@example.com"
    assert session.email_verified is True
    assert session.first_name == "Ada"
    assert session.last_name == "Lovelace"


@pytest.mark.unit
def test_decode_rejects_token_signed_with_different_secret() -> None:
    token, _ = encode_session_jwt(_user(), provider="google", secret=SECRET, algorithm=ALG, ttl_seconds=3600)

    with pytest.raises(InvalidSessionTokenError):
        decode_session_jwt(token, secret="a-different-but-equally-long-test-secret", algorithm=ALG)


@pytest.mark.unit
def test_decode_rejects_tampered_payload() -> None:
    token, _ = encode_session_jwt(_user(), provider="google", secret=SECRET, algorithm=ALG, ttl_seconds=3600)
    header, payload, sig = token.split(".")
    tampered = f"{header}.{payload[:-2]}AA.{sig}"

    with pytest.raises(InvalidSessionTokenError):
        decode_session_jwt(tampered, secret=SECRET, algorithm=ALG)


@pytest.mark.unit
def test_decode_rejects_expired_token() -> None:
    token, _ = encode_session_jwt(_user(), provider="google", secret=SECRET, algorithm=ALG, ttl_seconds=-1)

    with pytest.raises(InvalidSessionTokenError):
        decode_session_jwt(token, secret=SECRET, algorithm=ALG)


@pytest.mark.unit
def test_decode_rejects_malformed_garbage() -> None:
    with pytest.raises(InvalidSessionTokenError):
        decode_session_jwt("not-a-jwt", secret=SECRET, algorithm=ALG)


@pytest.mark.unit
def test_encode_refresh_jwt_produces_distinct_token_from_access() -> None:
    user = _user()

    access, _ = encode_session_jwt(user, provider="google", secret=SECRET, algorithm=ALG, ttl_seconds=3600)
    refresh = encode_refresh_jwt(user, provider="google", secret=SECRET, algorithm=ALG, ttl_seconds=86400)

    assert refresh != access
    assert refresh.count(".") == 2  # JWT has three segments


@pytest.mark.unit
def test_decode_session_jwt_rejects_refresh_token() -> None:
    refresh = encode_refresh_jwt(_user(), provider="google", secret=SECRET, algorithm=ALG, ttl_seconds=86400)

    with pytest.raises(InvalidSessionTokenError):
        decode_session_jwt(refresh, secret=SECRET, algorithm=ALG)


@pytest.mark.unit
def test_decode_refresh_jwt_roundtrip_preserves_identity() -> None:
    user = _user()
    user.roles = [Role(name="admin", permissions=["users:read", "users:delete"])]

    refresh = encode_refresh_jwt(user, provider="google", secret=SECRET, algorithm=ALG, ttl_seconds=86400)
    identity = decode_refresh_jwt(refresh, secret=SECRET, algorithm=ALG)

    assert identity.id == user.id
    assert identity.provider == "google"
    assert identity.email == "ada@example.com"
    assert identity.permissions == frozenset({"users:read", "users:delete"})


@pytest.mark.unit
def test_encode_refresh_jwt_defaults_auth_time_to_now() -> None:
    before = int(datetime.now(UTC).timestamp())
    refresh = encode_refresh_jwt(_user(), provider="google", secret=SECRET, algorithm=ALG, ttl_seconds=86400)
    after = int(datetime.now(UTC).timestamp())

    auth_time = decode_refresh_jwt(refresh, secret=SECRET, algorithm=ALG).auth_time
    assert auth_time is not None
    assert before <= auth_time <= after


@pytest.mark.unit
def test_encode_refresh_jwt_preserves_explicit_auth_time() -> None:
    refresh = encode_refresh_jwt(
        _user(), provider="google", secret=SECRET, algorithm=ALG, ttl_seconds=86400, auth_time=1_700_000_000
    )

    assert decode_refresh_jwt(refresh, secret=SECRET, algorithm=ALG).auth_time == 1_700_000_000


@pytest.mark.unit
def test_decode_carries_issued_at_on_both_token_types() -> None:
    user = _user()
    before = int(datetime.now(UTC).timestamp())
    access, _ = encode_session_jwt(user, provider="local", secret=SECRET, algorithm=ALG, ttl_seconds=3600)
    refresh = encode_refresh_jwt(user, provider="local", secret=SECRET, algorithm=ALG, ttl_seconds=86400)
    after = int(datetime.now(UTC).timestamp())

    access_iat = decode_session_jwt(access, secret=SECRET, algorithm=ALG).issued_at
    refresh_iat = decode_refresh_jwt(refresh, secret=SECRET, algorithm=ALG).issued_at

    assert access_iat is not None
    assert before <= access_iat <= after
    assert refresh_iat is not None
    assert before <= refresh_iat <= after


@pytest.mark.unit
def test_decode_refresh_jwt_rejects_access_token() -> None:
    access, _ = encode_session_jwt(_user(), provider="google", secret=SECRET, algorithm=ALG, ttl_seconds=3600)

    with pytest.raises(InvalidSessionTokenError):
        decode_refresh_jwt(access, secret=SECRET, algorithm=ALG)


@pytest.mark.unit
def test_decode_refresh_jwt_rejects_expired_token() -> None:
    refresh = encode_refresh_jwt(_user(), provider="google", secret=SECRET, algorithm=ALG, ttl_seconds=-1)

    with pytest.raises(InvalidSessionTokenError):
        decode_refresh_jwt(refresh, secret=SECRET, algorithm=ALG)


@pytest.mark.unit
def test_decode_refresh_jwt_rejects_wrong_secret() -> None:
    refresh = encode_refresh_jwt(_user(), provider="google", secret=SECRET, algorithm=ALG, ttl_seconds=86400)

    with pytest.raises(InvalidSessionTokenError):
        decode_refresh_jwt(refresh, secret="a-different-but-equally-long-test-secret", algorithm=ALG)


@pytest.mark.unit
def test_encode_decode_carries_permissions_from_loaded_roles() -> None:
    user = _user()
    user.roles = [
        Role(name="admin", permissions=["users:read", "users:delete"]),
        Role(name="editor", permissions=["users:read", "users:update"]),
    ]

    token, _ = encode_session_jwt(user, provider="local", secret=SECRET, algorithm=ALG, ttl_seconds=3600)
    session = decode_session_jwt(token, secret=SECRET, algorithm=ALG)

    assert session.permissions == frozenset({"users:read", "users:delete", "users:update"})


@pytest.mark.unit
def test_encode_decode_emits_empty_permissions_when_roles_not_loaded() -> None:
    token, _ = encode_session_jwt(_user(), provider="local", secret=SECRET, algorithm=ALG, ttl_seconds=3600)
    session = decode_session_jwt(token, secret=SECRET, algorithm=ALG)

    assert session.permissions == frozenset()


@pytest.mark.unit
def test_encode_decode_drops_permissions_from_soft_deleted_roles() -> None:
    user = _user()
    user.roles = [
        Role(name="admin", permissions=["users:read"], deleted_at=datetime.now(UTC)),
        Role(name="member", permissions=["users:read"]),
    ]

    token, _ = encode_session_jwt(user, provider="local", secret=SECRET, algorithm=ALG, ttl_seconds=3600)
    session = decode_session_jwt(token, secret=SECRET, algorithm=ALG)

    assert session.permissions == frozenset({"users:read"})


@pytest.mark.unit
def test_decode_rejects_non_list_permissions_claim() -> None:
    user = _user()
    now = int(datetime.now(UTC).timestamp())
    claims = {
        "sub": str(user.id),
        "provider": "local",
        "email": user.email,
        "email_verified": True,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "permissions": "users:read",
        "typ": "access",
        "iat": now,
        "exp": now + 3600,
    }
    token = joserfc_jwt.encode({"alg": ALG}, claims, OctKey.import_key(SECRET), algorithms=[ALG])

    with pytest.raises(InvalidSessionTokenError):
        decode_session_jwt(token, secret=SECRET, algorithm=ALG)
