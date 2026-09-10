"""At-rest sealing for conversation message text. Model credentials are `ai_gateway.crypto`, not this.

Stored as ``<kid>:v2:<base64(nonce ‖ ciphertext)>`` — AES-256-GCM, one key-id byte, a format version.

The key-id is what makes a rotation need no data migration: rows keep opening under the retired key
until `services/rewrap.py` (`make rewraptranscripts`) moves them, and only then may that key be
dropped. Associated data binds each value to this column, so a ciphertext from elsewhere pasted into
`messages.content` fails to open instead of being served as transcript text.

Synchronous despite the async-by-default rule: sealing is 3.4 µs, so there is no yield point to give.
"""

import base64
import binascii
import hashlib
import os
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

from app.core.config import Settings

_KID_LEN = 2  # 1 byte, hex-encoded
_HEX = frozenset("0123456789abcdef")
ENVELOPE = "v2:"
_NONCE_BYTES = 12  # AES-GCM's standard nonce size
_TAG_BYTES = 16  # GCM's authentication tag, appended to the ciphertext

# Binds a ciphertext to this column. Authenticated, not secret.
CONTENT_AAD = b"messages.content"

# An active secret plus the outgoing one kept readable during a rotation window.
SecretPair = tuple[SecretStr, SecretStr | None]


class ContentDecryptError(Exception):
    """Raised when stored message text fails to decrypt.

    Wraps the cipher layer's own errors so callers depend on one exception. Usual cause is a rotation
    that dropped the key the row was sealed under.
    """


def content_keys(settings: Settings) -> SecretPair:
    """The active/retired pair sealing message text — never the model-credential secrets."""
    return settings.conversation_secrets_key, settings.conversation_secrets_key_retired


@lru_cache
def _resolve(secret: SecretStr) -> tuple[str, bytes]:
    """Derive ``(key-id, 32-byte cipher key)`` from a configured secret.

    The key-id is a *second* hash of the cipher key, never the cipher key itself: deriving it from the
    first hash would publish a byte of the key on every stored row.
    """
    material = hashlib.sha256(secret.get_secret_value().encode()).digest()
    kid = hashlib.sha256(material).digest()[:1].hex()
    return kid, material


@lru_cache
def _aead(material: bytes) -> AESGCM:
    """The AES-GCM primitive for a derived key — memoized: building it per value is pure overhead."""
    return AESGCM(material)


def _keyring(settings: Settings) -> tuple[str, dict[str, bytes]]:
    """Resolve the configured pair into ``(active key-id, {kid: key})``.

    Raises:
        ValueError: The active and retired secrets collide on their 1-byte id (1/256) — failing loud
            beats mis-routing opens. `Settings` catches it at startup; this is the runtime backstop.
    """
    active, retired = content_keys(settings)
    active_kid, active_key = _resolve(active)
    ring = {active_kid: active_key}
    if retired is not None:
        kid, key = _resolve(retired)
        if kid == active_kid:
            raise ValueError("Active and retired conversation keys collide on key-id; choose a different retired key.")
        ring[kid] = key
    return active_kid, ring


def validate_content_keyring(settings: Settings) -> None:
    """Startup hook: building the keyring is the validation, so a key-id collision fails at boot."""
    _keyring(settings)


def active_kid(settings: Settings) -> str:
    """The key-id new values are tagged with; the sweep asks SQL for rows *not* carrying it."""
    kid, _ring = _keyring(settings)
    return kid


def _split(stored: str) -> tuple[str | None, str]:
    """Split a stored value into ``(key-id, payload)``; ``(None, value)`` if untagged.

    Only a leading 2-hex-char ``kid:`` prefix counts, so a payload starting with hex is not mistaken.
    """
    kid, sep, payload = stored.partition(":")
    if sep and len(kid) == _KID_LEN and all(c in _HEX for c in kid):
        return kid, payload
    return None, stored


def seal_content(plaintext: str, *, protected: bool, settings: Settings) -> tuple[str, bool]:
    """Return `(stored value, content_encrypted)` for a message about to be written.

    Both halves come from one call so the discriminator cannot disagree with the column. Empty text is
    stored as-is even when protected — a streaming placeholder is sealed once `finalize_message` fills
    it in.
    """
    if not protected or not plaintext:
        return plaintext, False
    kid, ring = _keyring(settings)
    nonce = os.urandom(_NONCE_BYTES)
    sealed = _aead(ring[kid]).encrypt(nonce, plaintext.encode(), CONTENT_AAD)
    return f"{kid}:{ENVELOPE}{base64.b64encode(nonce + sealed).decode()}", True


def unseal_content(stored: str, *, encrypted: bool, settings: Settings) -> str:
    """Plaintext for a stored value, keyed on the row's own discriminator.

    Raises:
        ContentDecryptError: `encrypted` is true and no configured key opens the value.
    """
    if not encrypted:
        return stored
    kid, payload = _split(stored)
    _active, ring = _keyring(settings)
    if not payload.startswith(ENVELOPE):
        # No marker means something else wrote it; refused rather than guessed at.
        raise ContentDecryptError("Stored message text is not in the expected envelope format.")
    candidates = [ring[kid]] if kid in ring else ([] if kid is not None else list(ring.values()))
    if not candidates:
        raise ContentDecryptError("Message text is sealed under a key-id that matches no configured key.")
    try:
        raw = base64.b64decode(payload[len(ENVELOPE) :], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ContentDecryptError("Stored message text is not valid base64.") from exc
    if len(raw) < _NONCE_BYTES + _TAG_BYTES:
        # Too short to hold a nonce and a tag, so `decrypt` would raise a library error, not InvalidTag.
        raise ContentDecryptError("Stored message text is too short to be a sealed value.")
    nonce, sealed = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    for key in candidates:
        try:
            return _aead(key).decrypt(nonce, sealed, CONTENT_AAD).decode()
        except InvalidTag:
            continue
    raise ContentDecryptError("Message text failed authentication under every configured key.")
