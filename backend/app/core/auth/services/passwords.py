"""Password hashing with Argon2id.

`PasswordHasher`'s defaults track the OWASP recommendation for Argon2id and
rotate as the library updates; we keep them rather than pinning parameters
here so we benefit from upstream changes without forking config.
"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError
from argon2.exceptions import VerificationError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash a plaintext password with Argon2id."""
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Return whether ``password`` matches the previously stored ``hashed`` value.

    Mismatches and any malformed-hash errors collapse to ``False`` — callers
    should treat verification failure uniformly rather than branching on the
    underlying cause. ``VerificationError`` covers ``VerifyMismatchError`` and
    other verification failures; ``InvalidHashError`` (a separate ``ValueError``
    subclass) covers unparseable hashes coming back from the DB.
    """
    try:
        return _hasher.verify(hashed, password)
    except VerificationError, InvalidHashError:
        return False
