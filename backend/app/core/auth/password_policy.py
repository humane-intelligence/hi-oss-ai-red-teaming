"""Password policy: length bounds on the schema types + a registry of content rules.

Length/cap live on the set-password Pydantic types so they reach the OpenAPI contract and
reject early; the content rules (configured minimum length, character classes, common-password
denylist, identity similarity) are a registry run by `validate_password` at the point a password
is *set* (register / invitation-accept / password-reset). Login carries no length bound and runs
none of these rules — it verifies an existing credential, so an empty or over-cap password
collapses to the same uniform 401 in the service (which guards the cap before argon2 to bound
work), never a 422 that would leak the policy. Similarity needs the account identity, which the
reset/invite schemas don't carry, so the registry runs in the service layer where the identity is
available. `hash_password` stays policy-free so internal provisioning (seed, admin bootstrap) is
exempt.

The admin-tunable half of the policy (`PasswordPolicy`) comes from the platform-settings
singleton, so it cannot live on the Pydantic types: the contract is one document for every
install. `MIN_PASSWORD_LENGTH` therefore stays the schema floor (a shorter password is a 422
before any rule runs) and a configured minimum above it is enforced here as a 400 carrying a
field-addressable code — the same shape the other content rules already use.
"""

import gzip
import re
from collections.abc import Callable
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Annotated
from typing import Self

from pydantic import Field
from pydantic import SecretStr

from app.core.exceptions import BadRequestError
from app.core.platform_settings.models import PlatformSettings
from app.core.schemas import ProblemErrorItem

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
_SIMILARITY_THRESHOLD = 0.7
_MIN_IDENTITY_PART_LENGTH = 3

NewPassword = Annotated[SecretStr, Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)]


@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    """The admin-tunable rules; the defaults are what the platform ships with.

    Instantiating it bare is the shipped policy, which is what internal callers and tests want;
    request paths build it from the platform settings so an admin override actually applies.
    """

    min_length: int = MIN_PASSWORD_LENGTH
    require_uppercase: bool = False
    require_digit: bool = False
    require_symbol: bool = False

    @classmethod
    def from_settings(cls, settings: PlatformSettings) -> Self:
        return cls(
            min_length=settings.password_min_length,
            require_uppercase=settings.password_require_uppercase,
            require_digit=settings.password_require_digit,
            require_symbol=settings.password_require_symbol,
        )


class PasswordPolicyError(BadRequestError):
    """A new password failed a content rule — 400.

    Carries a field-addressable `errors[]` entry (the same shape a 422 uses) pointing at the
    `password` field, with a machine-readable code in `type` — one per rule in
    `PASSWORD_VALIDATORS`, so a client maps the failure inline onto the field and can localize
    it. The codes are contract; the route docstrings list them.
    """

    def __init__(self, detail: str, *, code: str) -> None:
        super().__init__(detail)
        self.errors = [ProblemErrorItem(loc=["body", "password"], msg=detail, type=code)]


def _load_denylist() -> frozenset[str]:
    path = Path(__file__).parent / "data" / "common_passwords.txt.gz"
    try:
        text = gzip.decompress(path.read_bytes()).decode("utf-8")
    except OSError as exc:
        raise RuntimeError(f"password denylist missing or unreadable: {path}") from exc
    return frozenset(entry for line in text.splitlines() if (entry := line.strip().casefold()))


_DENYLIST = _load_denylist()


def _reject_too_short(password: str, identity: Sequence[str | None], policy: PasswordPolicy) -> None:
    if len(password) < policy.min_length:
        raise PasswordPolicyError(
            f"Password must be at least {policy.min_length} characters long.",
            code="password_too_short",
        )


def _reject_missing_character_class(password: str, identity: Sequence[str | None], policy: PasswordPolicy) -> None:
    # A symbol is anything that is neither a letter nor a digit, so the rule doesn't quietly
    # exclude non-ASCII keyboards the way a fixed punctuation set would.
    requirements = (
        (policy.require_uppercase, any(char.isupper() for char in password), "an uppercase letter", "uppercase"),
        (policy.require_digit, any(char.isdigit() for char in password), "a digit", "digit"),
        (policy.require_symbol, any(not char.isalnum() for char in password), "a symbol", "symbol"),
    )
    for required, satisfied, description, code in requirements:
        if required and not satisfied:
            raise PasswordPolicyError(
                f"Password must contain {description}.",
                code=f"password_missing_{code}",
            )


def _reject_common(password: str, identity: Sequence[str | None], policy: PasswordPolicy) -> None:
    if password.casefold() in _DENYLIST:
        raise PasswordPolicyError(
            "This password is too common or has appeared in a known data breach.",
            code="password_too_common",
        )


def _reject_similar_to_identity(password: str, identity: Sequence[str | None], policy: PasswordPolicy) -> None:
    candidate = password.casefold()
    for value in identity:
        if not value:
            continue
        folded = value.casefold()
        tokens = [folded, *(part for part in re.split(r"\W+", folded) if len(part) >= _MIN_IDENTITY_PART_LENGTH)]
        if any(SequenceMatcher(a=candidate, b=token).quick_ratio() >= _SIMILARITY_THRESHOLD for token in tokens):
            raise PasswordPolicyError(
                "This password is too similar to your email or name.",
                code="password_too_similar",
            )


PasswordValidator = Callable[[str, Sequence[str | None], PasswordPolicy], None]

# Cheapest and most actionable first: a user retyping a too-short password shouldn't have to
# clear the denylist rule to be told about the length.
PASSWORD_VALIDATORS: tuple[PasswordValidator, ...] = (
    _reject_too_short,
    _reject_missing_character_class,
    _reject_common,
    _reject_similar_to_identity,
)


def validate_password(password: str, *, identity: Sequence[str | None] = (), policy: PasswordPolicy) -> None:
    """Run every content rule against a new password; raise on the first failure.

    `policy` is deliberately required: a request path that forgot to load it would otherwise
    silently enforce the shipped defaults instead of the admin's settings.

    Args:
        password: The plaintext password being set.
        identity: Account strings (email, names) the password must not resemble;
            falsy entries are skipped.
        policy: The admin-tunable rules to enforce (see `PasswordPolicy.from_settings`).
    """
    for validate in PASSWORD_VALIDATORS:
        validate(password, identity, policy)
