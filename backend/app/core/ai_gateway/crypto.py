"""At-rest encryption for `AiModel.api_key_encrypted` via JWE (`dir` + `A256GCM`).

The cipher key is the 32-byte SHA-256 digest of a configured secret, so any
≥32-char value works without forcing operators to hand-generate a base64-encoded
256-bit blob. Stored form is `<kid>:<jwe>` — a 1-byte key-id (hex-encoded, 2
chars) prefixing a JWE compact-serialized string with a fresh IV per call, so
re-encrypting the same plaintext yields a different ciphertext.

The key-id enables a dual-key rotation window with no data migration: set the
incoming key as `MODEL_SECRETS_KEY` and keep the outgoing one as
`MODEL_SECRETS_KEY_RETIRED`. New writes carry the active key's id; rows written
under the retired key still decrypt (matched by id) while a background job
re-wraps them — see `needs_rewrap`. Once nothing needs re-wrapping, drop the
retired key. The id is derived from a *second* hash of the cipher key, never the
cipher key itself, so it leaks nothing about the key.

Ciphertexts written before the key-id existed carry no prefix; they are decrypted
by trying every configured key in turn (active first, then retired). A256GCM
authentication makes a wrong-key attempt fail cleanly, so this never mis-decrypts
— and it keeps legacy rows readable through a rotation, when the key that wrote
them has already moved to the retired slot. A *tagged* ciphertext whose id matches
no configured key fails as `SecretDecryptError` rather than being silently
mis-routed.
"""

import hashlib
from functools import lru_cache

from joserfc import jwe
from joserfc.errors import JoseError
from joserfc.jwk import OctKey
from pydantic import SecretStr

from app.core.config import Settings

_ALG = "dir"
_ENC = "A256GCM"
_PROTECTED = {"alg": _ALG, "enc": _ENC}
_KID_LEN = 2  # 1 byte, hex-encoded
_HEX = frozenset("0123456789abcdef")


class SecretDecryptError(Exception):
    """Raised when a stored ciphertext fails to decrypt.

    Wraps `joserfc.errors.JoseError` so callers don't depend on the JOSE
    library's exception hierarchy. Most likely cause is the ciphertext's key-id
    matching no configured key (a rotation that dropped the retired key);
    possible causes also include corruption or hand-edited rows.
    """


@lru_cache
def _resolve(secret: SecretStr) -> tuple[str, OctKey]:
    """Derive ``(key-id, cipher key)`` from a configured secret.

    The cipher key is the SHA-256 digest of the secret; the public key-id is a
    *second* hash of that digest, truncated to 1 byte. Hashing again keeps the
    id independent of the AES key by preimage resistance — deriving it from the
    first hash would publish a byte of the cipher key on every stored row.

    Memoized on the (hashable) secret: derivation is pure, and `_keyring` rebuilds
    it on every encrypt/decrypt/needs_rewrap — three times per row in the re-wrap
    job. Only the active + retired secrets ever appear, so the cache stays tiny;
    holding the derived key is no more exposure than `Settings` already carries.
    """
    material = hashlib.sha256(secret.get_secret_value().encode()).digest()
    kid = hashlib.sha256(material).digest()[:1].hex()
    return kid, OctKey.import_key(material)


def _keyring(settings: Settings) -> tuple[str, dict[str, OctKey]]:
    """Resolve configured secrets into the active key-id and a decrypt lookup.

    Returns:
        ``(active_kid, ring)`` where ``ring`` is a ``{kid: key}`` map of every
        configured key (active first, then retired). New encryptions use
        ``ring[active_kid]``; decrypts consult the whole map.

    Raises:
        ValueError: If the active and retired keys collide on their 1-byte id
            (1/256 chance) — failing loud beats silently mis-routing decrypts.
            Normally caught at startup by `Settings` validation; the check is
            repeated here as a runtime backstop.
    """
    active_kid, active_key = _resolve(settings.model_secrets_key)
    ring = {active_kid: active_key}
    retired = settings.model_secrets_key_retired
    if retired is not None:
        kid, key = _resolve(retired)
        if kid == active_kid:
            raise ValueError("Active and retired model secret keys collide on key-id; choose a different retired key.")
        ring[kid] = key
    return active_kid, ring


def validate_keyring(settings: Settings) -> None:
    """Public startup hook: surface an active/retired key-id collision as a config error.

    Building the keyring is the validation — it raises `ValueError` on a
    collision. Exposed (rather than importing `_keyring`) so the config→crypto
    startup check depends on a public name, not crypto internals. See
    `Settings._validate_model_secret_keyring`.
    """
    _keyring(settings)


def _split(ciphertext: str) -> tuple[str | None, str]:
    """Split a stored value into ``(key-id, jwe)``; ``(None, value)`` if untagged.

    Untagged is the legacy form (written before the key-id existed). A JWE
    compact string is base64url + dots, so it never contains ``:``; only a
    leading 2-hex-char ``kid:`` prefix is treated as a tag.
    """
    kid, sep, payload = ciphertext.partition(":")
    if sep and len(kid) == _KID_LEN and all(c in _HEX for c in kid):
        return kid, payload
    return None, ciphertext


def encrypt_secret(plaintext: str, settings: Settings) -> str:
    """Encrypt `plaintext` under the configured `MODEL_SECRETS_KEY`.

    Args:
        plaintext: Raw secret to wrap (e.g. an API key). Must be non-empty.
        settings: Application settings carrying `model_secrets_key`.

    Returns:
        A ``<kid>:<jwe>`` string safe to store in a `VARCHAR(1024)` column —
        all base64url + dots + a 2-hex-char prefix, so ASCII throughout.
    """
    if not plaintext:
        raise ValueError("Cannot encrypt an empty secret.")
    active_kid, ring = _keyring(settings)
    return f"{active_kid}:{jwe.encrypt_compact(_PROTECTED, plaintext.encode(), ring[active_kid])}"


def decrypt_secret(ciphertext: str, settings: Settings) -> str:
    """Decrypt a value produced by `encrypt_secret`.

    Selects the key by the ciphertext's key-id. An untagged (legacy) ciphertext
    has no id to match, so every configured key is tried in turn (active first,
    then retired); A256GCM authentication rejects the wrong key, so this is
    unambiguous and keeps legacy rows readable through a rotation.

    Raises:
        SecretDecryptError: If the ciphertext is malformed, tampered with, or
            tagged with a key-id that matches no configured key (e.g. a rotation
            that dropped the retired key).
    """
    _active_kid, ring = _keyring(settings)
    kid, payload = _split(ciphertext)
    if kid is None:
        candidates = list(ring.values())  # active first by insertion order
    elif kid in ring:
        candidates = [ring[kid]]
    else:
        raise SecretDecryptError(f"No key configured for key-id {kid!r}.")
    last_exc: Exception | None = None
    for key in candidates:
        try:
            # Pin alg/enc so a ciphertext that claims a weaker primitive in its header is rejected.
            result = jwe.decrypt_compact(payload, key, algorithms=[_ALG, _ENC])
        except (JoseError, ValueError) as exc:
            last_exc = exc
            continue
        if result.plaintext is None:
            raise SecretDecryptError("Decrypted payload was empty.")
        return result.plaintext.decode()
    raise SecretDecryptError(str(last_exc)) from last_exc


def needs_rewrap(ciphertext: str, settings: Settings) -> bool:
    """Return whether ``ciphertext`` is not tagged with the active key's id.

    Drives the rotation re-wrap job: a ciphertext is stale if it is untagged
    (predates the key-id) or tagged with the retired key.
    """
    active_kid, _ = _keyring(settings)
    kid, _payload = _split(ciphertext)
    return kid != active_kid
