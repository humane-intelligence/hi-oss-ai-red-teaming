from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import uuid4

import pytest
import time_machine
from itsdangerous import BadSignature
from itsdangerous import SignatureExpired

from app.core.media.signing import _serializer
from app.core.media.signing import sign_key
from app.core.media.signing import verify_token

_KEY = "2026/07/09/a1b2c3d4111122223333444455556666.png"


def _future_exp() -> int:
    return int((datetime.now(UTC) + timedelta(seconds=900)).timestamp())


@pytest.mark.unit
def test_sign_key_round_trips() -> None:
    token, _ = sign_key(_KEY, ttl=900)

    key, _expires_at, user_id = verify_token(token)

    assert (key, user_id) == (_KEY, None)


@pytest.mark.unit
def test_sign_key_returns_expiry_ttl_ahead() -> None:
    start = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)

    with time_machine.travel(start, tick=False):
        _, expires_at = sign_key(_KEY, ttl=900)

    assert expires_at == start + timedelta(seconds=900)


@pytest.mark.unit
def test_verify_token_returns_embedded_expiry() -> None:
    start = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)

    with time_machine.travel(start, tick=False):
        token, _ = sign_key(_KEY, ttl=900)
        _key, expires_at, _user_id = verify_token(token)

    assert expires_at == start + timedelta(seconds=900)


@pytest.mark.unit
def test_verify_token_enforces_minted_ttl() -> None:
    # The expiry embedded at mint is authoritative — not any TTL in force at verify time.
    start = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)

    with time_machine.travel(start, tick=False) as traveller:
        token, _ = sign_key(_KEY, ttl=60)
        traveller.shift(timedelta(seconds=61))
        with pytest.raises(SignatureExpired):
            verify_token(token)


@pytest.mark.unit
def test_verify_token_accepts_within_minted_ttl() -> None:
    start = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)

    with time_machine.travel(start, tick=False) as traveller:
        token, _ = sign_key(_KEY, ttl=60)
        traveller.shift(timedelta(seconds=59))
        key, _expires_at, _user_id = verify_token(token)

    assert key == _KEY


@pytest.mark.unit
def test_verify_token_returns_bound_user() -> None:
    user_id = uuid4()
    token, _ = sign_key(_KEY, ttl=900, user_id=user_id)

    key, _expires_at, bound = verify_token(token)

    assert (key, bound) == (_KEY, user_id)


@pytest.mark.unit
def test_verify_token_rejects_tampered() -> None:
    token, _ = sign_key(_KEY, ttl=900)

    with pytest.raises(BadSignature):
        verify_token(token + "tampered")


@pytest.mark.unit
def test_verify_token_rejects_unexpected_payload_shape() -> None:
    # A validly-signed token whose payload lacks the "k" key must 403 (BadSignature), not 500 (KeyError).
    token = _serializer().dumps({"unexpected": "shape"})

    with pytest.raises(BadSignature):
        verify_token(token)


@pytest.mark.unit
def test_verify_token_rejects_legacy_payload_without_exp() -> None:
    # Tokens minted before the exp claim carried only {"k": key}; without an embedded expiry they must 403.
    token = _serializer().dumps({"k": _KEY})

    with pytest.raises(BadSignature):
        verify_token(token)


@pytest.mark.unit
@pytest.mark.parametrize("bad_user", ["not-a-uuid", 123])
def test_verify_token_rejects_malformed_user_claim(bad_user: str | int) -> None:
    # A validly-signed token with a u claim that isn't a UUID must 403, not 500.
    token = _serializer().dumps({"k": _KEY, "exp": _future_exp(), "u": bad_user})

    with pytest.raises(BadSignature):
        verify_token(token)
