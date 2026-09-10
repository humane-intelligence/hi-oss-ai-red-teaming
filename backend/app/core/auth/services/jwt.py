"""Stateless session JWT — sign on login, verify on every request."""

import time
from typing import Any
from uuid import UUID

from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import OctKey
from joserfc.jwt import JWTClaimsRegistry

from app.core.auth.models import User
from app.core.auth.schemas import SessionUser
from app.core.auth.schemas import TokenResponse
from app.core.auth.services.roles import effective_permissions
from app.core.config import Settings

# `typ` claim values that distinguish access vs refresh tokens. Mixing them up
# would let a refresh token act as a long-lived access token (or vice versa),
# so every decoder gates on the value it expects.
_TYP_ACCESS = "access"
_TYP_REFRESH = "refresh"  # nosec B105 — claim value, not a credential


class InvalidSessionTokenError(Exception):
    """Raised when a bearer token fails signature, claims, or shape checks."""


def _key(secret: str) -> OctKey:
    return OctKey.import_key(secret)


def _identity_claims(user: User, *, provider: str) -> dict[str, Any]:
    return {
        "sub": str(user.id),
        "provider": provider,
        "email": user.email,
        "email_verified": bool(user.email_verified_at),
        "first_name": user.first_name,
        "last_name": user.last_name,
        "permissions": effective_permissions(user),
    }


def encode_session_jwt(user: User, *, provider: str, secret: str, algorithm: str, ttl_seconds: int) -> tuple[str, int]:
    """Mint a session (access) JWT for `user` authenticated via `provider`.

    Embeds ``sub`` / ``provider`` / ``email`` / ``email_verified`` /
    ``first_name`` / ``last_name`` / ``permissions`` plus ``iat`` and ``exp``
    so the bearer token alone is enough to materialize a `SessionUser` —
    middleware never has to touch the database. ``permissions`` is flattened
    from `user.roles[*].permissions` at mint time, which requires the
    relationship to be eager-loaded (otherwise the claim ends up empty).

    Args:
        user: Internal user whose identity is being captured. `user.roles`
            must be eager-loaded for the `permissions` claim to be populated.
        provider: Mechanism that authenticated this session — OIDC provider
            name (e.g. ``"google"``) or ``"local"`` for password auth. Passed
            separately because it describes the current login, not the user
            (a single user may have multiple `ProviderIdentity` rows).
        secret: HMAC signing secret. Must be long enough for the chosen
            algorithm (HS256 requires >= 32 bytes).
        algorithm: JWS algorithm header (e.g. ``"HS256"``).
        ttl_seconds: Token lifetime; ``exp`` is set to ``iat + ttl_seconds``.

    Returns:
        A ``(token, expires_in)`` tuple where ``token`` is the signed JWT
        and ``expires_in`` echoes ``ttl_seconds`` for convenience in
        ``TokenResponse`` payloads.
    """
    now = int(time.time())
    claims: dict[str, Any] = {
        **_identity_claims(user, provider=provider),
        "typ": _TYP_ACCESS,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    token = jwt.encode({"alg": algorithm}, claims, _key(secret), algorithms=[algorithm])
    return token, ttl_seconds


def encode_refresh_jwt(
    user: User,
    *,
    provider: str,
    secret: str,
    algorithm: str,
    ttl_seconds: int,
    auth_time: int | None = None,
) -> str:
    """Mint a refresh JWT — identity claims plus `typ="refresh"`, a longer `exp`, and `auth_time`.

    `auth_time` is the epoch-seconds instant of the *original* login, carried
    unchanged across rotations so `/auth/refresh` can enforce an absolute session
    ceiling. It defaults to now (the initial-login case); the refresh re-mint
    passes the value decoded from the incoming token.
    """
    now = int(time.time())
    claims: dict[str, Any] = {
        **_identity_claims(user, provider=provider),
        "typ": _TYP_REFRESH,
        "iat": now,
        "exp": now + ttl_seconds,
        "auth_time": auth_time if auth_time is not None else now,
    }
    return jwt.encode({"alg": algorithm}, claims, _key(secret), algorithms=[algorithm])


def _decode_verified_claims(token: str, *, secret: str, algorithm: str) -> dict[str, Any]:
    """Verify the JWS signature + registered claims (notably ``exp``); return the raw claims."""
    try:
        decoded = jwt.decode(token, _key(secret), algorithms=[algorithm])
        JWTClaimsRegistry().validate(decoded.claims)
    except JoseError as exc:
        raise InvalidSessionTokenError(str(exc)) from exc
    return decoded.claims


def _session_user_from_claims(claims: dict[str, Any]) -> SessionUser:
    """Unpack verified identity claims into a `SessionUser`, validating the ``permissions`` shape."""
    raw_permissions = claims.get("permissions", [])
    if not isinstance(raw_permissions, list) or not all(isinstance(p, str) for p in raw_permissions):
        raise InvalidSessionTokenError("permissions claim must be a list of strings")
    try:
        return SessionUser(
            id=UUID(claims["sub"]),
            provider=claims["provider"],
            email=claims["email"],
            email_verified=bool(claims.get("email_verified", False)),
            first_name=claims.get("first_name"),
            last_name=claims.get("last_name"),
            permissions=frozenset[str](raw_permissions),
            auth_time=claims.get("auth_time"),
            issued_at=claims.get("iat"),
        )
    except (KeyError, ValueError) as exc:
        raise InvalidSessionTokenError(f"malformed claims: {exc}") from exc


def decode_session_jwt(token: str, *, secret: str, algorithm: str) -> SessionUser:
    """Verify signature + `exp` + `typ=="access"`, return the embedded `SessionUser`.

    Checks the JWS signature and the registered claim set (notably ``exp``),
    enforces ``typ=="access"`` so a refresh token can't pose as an access
    token, then unpacks the identity claims. The ``permissions`` claim must
    be a list of strings — anything else is treated as a malformed token.
    Every failure path raises `InvalidSessionTokenError` — callers should
    treat all failures uniformly and downgrade the request to anonymous
    rather than branching on the underlying cause.

    Args:
        token: Raw bearer token from the ``Authorization`` header.
        secret: HMAC signing secret used at mint time.
        algorithm: JWS algorithm to enforce (e.g. ``"HS256"``).

    Returns:
        A `SessionUser` materialized from the verified claims; ``permissions``
        is the frozen set drawn from the claim.

    Raises:
        InvalidSessionTokenError: For any signature, expiry, shape, missing-
            claim, or malformed-``permissions`` failure.
    """
    claims = _decode_verified_claims(token, secret=secret, algorithm=algorithm)
    if claims.get("typ") != _TYP_ACCESS:
        raise InvalidSessionTokenError("token is not an access token")
    return _session_user_from_claims(claims)


def decode_refresh_jwt(token: str, *, secret: str, algorithm: str) -> SessionUser:
    """Verify signature + `exp` + `typ=="refresh"`, return the embedded identity.

    The refresh counterpart of `decode_session_jwt`: same signature/expiry/shape
    checks, but gates on ``typ=="refresh"`` so an access token can't be replayed
    at the refresh endpoint. The returned `SessionUser` carries the identity the
    token was minted with — callers re-read the live user from the DB before
    minting a new pair, so its ``permissions`` are only as fresh as that token.

    Raises:
        InvalidSessionTokenError: For any signature, expiry, ``typ``, shape,
            missing-claim, or malformed-``permissions`` failure.
    """
    claims = _decode_verified_claims(token, secret=secret, algorithm=algorithm)
    if claims.get("typ") != _TYP_REFRESH:
        raise InvalidSessionTokenError("token is not a refresh token")
    return _session_user_from_claims(claims)


def mint_token_pair(user: User, *, provider: str, settings: Settings, auth_time: int | None = None) -> TokenResponse:
    """Sign the access + refresh JWTs for `user` and pack them into a `TokenResponse`.

    `auth_time` (original-login instant, epoch seconds) is stamped into the
    refresh token. Omit on initial login (defaults to now); the refresh re-mint
    passes the carried value so the absolute session ceiling survives rotation.
    """
    secret = settings.session_jwt_secret.get_secret_value()
    algorithm = settings.session_jwt_algorithm

    access_token, expires_in = encode_session_jwt(
        user,
        provider=provider,
        secret=secret,
        algorithm=algorithm,
        ttl_seconds=settings.session_ttl_seconds,
    )
    refresh_token = encode_refresh_jwt(
        user,
        provider=provider,
        secret=secret,
        algorithm=algorithm,
        ttl_seconds=settings.refresh_token_ttl_seconds,
        auth_time=auth_time,
    )
    return TokenResponse(access_token=access_token, refresh_token=refresh_token, expires_in=expires_in)
