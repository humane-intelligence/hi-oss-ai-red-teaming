"""Unit tests for `app.core.ai_gateway.crypto`."""

import pytest
from pydantic import SecretStr

from app.core.ai_gateway.crypto import SecretDecryptError
from app.core.ai_gateway.crypto import decrypt_secret
from app.core.ai_gateway.crypto import encrypt_secret
from app.core.ai_gateway.crypto import needs_rewrap
from app.core.config import Settings

_OLD = "old-model-secrets-key-at-least-32-chars"
_NEW = "new-model-secrets-key-at-least-32-chars"


def _settings(
    secret: str = "test-model-secrets-key-at-least-32-chars",  # noqa: S107 — test-only sentinel, not a real credential
    retired: str | None = None,
) -> Settings:
    return Settings.model_construct(
        model_secrets_key=SecretStr(secret),
        model_secrets_key_retired=SecretStr(retired) if retired is not None else None,
    )


@pytest.mark.unit
def test_encrypt_decrypt_round_trip_returns_original_plaintext() -> None:
    settings = _settings()

    token = encrypt_secret("sk-ant-very-secret", settings)
    plaintext = decrypt_secret(token, settings)

    assert plaintext == "sk-ant-very-secret"


@pytest.mark.unit
def test_encrypt_produces_fresh_ciphertext_each_call() -> None:
    """JWE `A256GCM` uses a fresh IV every call, so identical inputs yield distinct ciphertexts."""
    settings = _settings()

    first = encrypt_secret("same-input", settings)
    second = encrypt_secret("same-input", settings)

    assert first != second
    assert decrypt_secret(first, settings) == decrypt_secret(second, settings) == "same-input"


@pytest.mark.unit
def test_encrypt_rejects_empty_plaintext() -> None:
    with pytest.raises(ValueError, match="empty"):
        encrypt_secret("", _settings())


@pytest.mark.unit
def test_decrypt_with_wrong_key_raises() -> None:
    """A ciphertext from one key cannot be decrypted under another — emulates a `MODEL_SECRETS_KEY` rotation."""
    original = _settings("original-secret-at-least-32-chars-long")
    rotated = _settings("rotated-secret-at-least-32-chars-long-")

    token = encrypt_secret("payload", original)

    with pytest.raises(SecretDecryptError):
        decrypt_secret(token, rotated)


@pytest.mark.unit
def test_decrypt_rejects_tampered_ciphertext() -> None:
    settings = _settings()
    token = encrypt_secret("payload", settings)
    # Flip a character in the ciphertext segment to break the auth tag.
    tampered = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]

    with pytest.raises(SecretDecryptError):
        decrypt_secret(tampered, settings)


@pytest.mark.unit
def test_decrypt_rejects_malformed_string() -> None:
    with pytest.raises(SecretDecryptError):
        decrypt_secret("not-a-jwe", _settings())


@pytest.mark.unit
def test_ciphertext_is_prefixed_with_hex_key_id() -> None:
    token = encrypt_secret("payload", _settings())

    kid, sep, _jwe = token.partition(":")

    assert sep == ":"
    assert len(kid) == 2
    assert all(c in "0123456789abcdef" for c in kid)


@pytest.mark.unit
def test_legacy_untagged_ciphertext_decrypts_under_active_key() -> None:
    """Rows written before the key-id existed carry no prefix and still decrypt."""
    settings = _settings()
    legacy = encrypt_secret("payload", settings).split(":", 1)[1]

    assert decrypt_secret(legacy, settings) == "payload"


@pytest.mark.unit
def test_legacy_untagged_ciphertext_decrypts_during_rotation() -> None:
    """A legacy row written under the now-retired key must survive a rotation.

    Untagged ciphertexts have no id to route by, so decrypt falls back across
    the keyring — without this, the key that wrote them having moved to the
    retired slot would make them unreadable (and unrescuable by the re-wrap job).
    """
    legacy = encrypt_secret("payload", _settings(_OLD)).split(":", 1)[1]
    rotated = _settings(_NEW, retired=_OLD)

    assert decrypt_secret(legacy, rotated) == "payload"


@pytest.mark.unit
def test_rotation_window_decrypts_ciphertext_written_under_retired_key() -> None:
    old = _settings(_OLD)
    rotated = _settings(_NEW, retired=_OLD)

    token = encrypt_secret("payload", old)

    assert decrypt_secret(token, rotated) == "payload"


@pytest.mark.unit
def test_new_writes_use_active_key_id_not_the_retired_one() -> None:
    old = _settings(_OLD)
    rotated = _settings(_NEW, retired=_OLD)

    old_kid = encrypt_secret("payload", old).partition(":")[0]
    new_kid = encrypt_secret("payload", rotated).partition(":")[0]

    assert new_kid != old_kid


@pytest.mark.unit
def test_decrypt_rejects_unknown_key_id() -> None:
    settings = _settings()
    token = encrypt_secret("payload", settings)
    active_kid, _, jwe = token.partition(":")
    unknown_kid = "00" if active_kid != "00" else "01"

    with pytest.raises(SecretDecryptError, match="No key configured"):
        decrypt_secret(f"{unknown_kid}:{jwe}", settings)


@pytest.mark.unit
def test_colliding_active_and_retired_keys_raise() -> None:
    settings = _settings(_NEW, retired=_NEW)

    with pytest.raises(ValueError, match="collide on key-id"):
        encrypt_secret("payload", settings)


@pytest.mark.unit
def test_needs_rewrap_flags_retired_and_legacy_but_not_active() -> None:
    rotated = _settings(_NEW, retired=_OLD)
    under_retired = encrypt_secret("payload", _settings(_OLD))
    under_active = encrypt_secret("payload", rotated)
    legacy = under_active.split(":", 1)[1]

    assert needs_rewrap(under_retired, rotated) is True
    assert needs_rewrap(legacy, rotated) is True
    assert needs_rewrap(under_active, rotated) is False
