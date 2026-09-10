"""Sign + verify time-limited access tokens for media assets.

A token wraps the asset's storage `key` plus its absolute expiry, embedded at mint —
so the TTL in force when the token was issued is what `verify_token` enforces, each
mint site may pick its own TTL, and a later settings change never re-times an
outstanding token. A private asset's token additionally carries the requesting
user's id (`u`); the fetch route rejects any other caller. The signing key is
`media_url_signing_secret` when set, else `session_jwt_secret`; a fixed salt
domain-separates it from any other itsdangerous consumer of the same secret. This is
deliberately NOT an S3-presigned URL — the token rides the backend-agnostic
proxy-GET. The token is signed, not encrypted: its payload base64-decodes in the
clear, so nothing secret may ride it. `u` qualifies — it is the requester's own
non-secret id, readable only by whoever already holds the URL.
"""

from datetime import UTC
from datetime import datetime
from uuid import UUID

from itsdangerous import BadSignature
from itsdangerous import SignatureExpired
from itsdangerous import URLSafeTimedSerializer

from app.core.config import get_settings

_SALT = "media-url"


def _serializer() -> URLSafeTimedSerializer:
    settings = get_settings()
    secret = settings.media_url_signing_secret or settings.session_jwt_secret
    return URLSafeTimedSerializer(secret.get_secret_value(), salt=_SALT)


def sign_key(key: str, *, ttl: int, user_id: UUID | None = None) -> tuple[str, datetime]:
    """Sign `key` into an opaque token and return it with its expiry instant.

    Args:
        key: The asset storage key to embed.
        ttl: Seconds the token stays valid; the resulting absolute expiry is embedded
            in the signed payload and is exactly what `verify_token` enforces.
        user_id: When set, binds the token to that user — `verify_token` surfaces the
            id and the fetch route rejects any other (or anonymous) caller.
    """
    # Whole-second expiry so the returned instant and the embedded claim agree exactly.
    expires_at = datetime.fromtimestamp(int(datetime.now(UTC).timestamp()) + ttl, tz=UTC)
    payload: dict[str, str | int] = {"k": key, "exp": int(expires_at.timestamp())}
    if user_id is not None:
        payload["u"] = str(user_id)
    return _serializer().dumps(payload), expires_at


def verify_token(token: str) -> tuple[str, datetime, UUID | None]:
    """Return the token's key, expiry instant, and bound user id (None = any caller).

    Raises:
        itsdangerous.SignatureExpired: The token's embedded expiry has passed.
        itsdangerous.BadSignature: The token is malformed or forged.
    """
    payload = _serializer().loads(token)
    # A valid signature over an unexpected payload shape (e.g. an older token from
    # before `exp` was embedded) must stay a 403, not surface as a 500.
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("k"), str)
        or not isinstance(payload.get("exp"), int)
    ):
        raise BadSignature("Unexpected media token payload.")
    user_id: UUID | None = None
    if "u" in payload:
        if not isinstance(payload["u"], str):
            raise BadSignature("Unexpected media token payload.")
        try:
            user_id = UUID(payload["u"])
        except ValueError:
            raise BadSignature("Unexpected media token payload.") from None
    expires_at = datetime.fromtimestamp(payload["exp"], tz=UTC)
    if datetime.now(UTC) > expires_at:
        raise SignatureExpired("The signed URL has expired.")
    return payload["k"], expires_at, user_id
