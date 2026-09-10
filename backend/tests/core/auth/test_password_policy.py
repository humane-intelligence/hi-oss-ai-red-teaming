from uuid import uuid4

import pytest
from fastapi import status
from pydantic import BaseModel
from pydantic import SecretStr
from pydantic import ValidationError
from sqlalchemy import DefaultClause
from sqlmodel import SQLModel

from app.core.auth.password_policy import MIN_PASSWORD_LENGTH
from app.core.auth.password_policy import NewPassword
from app.core.auth.password_policy import PasswordPolicy
from app.core.auth.password_policy import PasswordPolicyError
from app.core.auth.password_policy import validate_password
from app.core.platform_settings.models import PlatformSettings

_STRONG = "a-very-uncommon-passphrase-42"
_SHIPPED = PasswordPolicy()


class _NewModel(BaseModel):
    password: NewPassword


@pytest.mark.unit
def test_new_password_accepts_strong_value() -> None:
    assert _NewModel(password=SecretStr(_STRONG)).password.get_secret_value() == _STRONG


@pytest.mark.unit
@pytest.mark.parametrize("value", ["abc", "x" * 7])
def test_new_password_rejects_below_minimum(value: str) -> None:
    with pytest.raises(ValidationError):
        _NewModel(password=SecretStr(value))


@pytest.mark.unit
def test_new_password_rejects_above_maximum() -> None:
    with pytest.raises(ValidationError):
        _NewModel(password=SecretStr("x" * (128 + 1)))


@pytest.mark.unit
def test_validate_password_accepts_strong_value() -> None:
    validate_password(_STRONG, identity=["ada@example.com", "Ada", "Lovelace"], policy=_SHIPPED)


@pytest.mark.unit
@pytest.mark.parametrize("value", ["password1234", "PASSWORD1234"])
def test_validate_password_rejects_common_case_insensitively(value: str) -> None:
    with pytest.raises(PasswordPolicyError):
        validate_password(value, policy=_SHIPPED)


@pytest.mark.unit
def test_validate_password_rejects_value_equal_to_email() -> None:
    with pytest.raises(PasswordPolicyError):
        validate_password("ada.lovelace@example.com", identity=["ada.lovelace@example.com"], policy=_SHIPPED)


@pytest.mark.unit
def test_validate_password_rejects_value_similar_to_name() -> None:
    with pytest.raises(PasswordPolicyError):
        validate_password("AdaLovelace-pw", identity=["Ada Lovelace"], policy=_SHIPPED)


@pytest.mark.unit
def test_password_policy_error_is_field_addressable_400() -> None:
    # 400, but field-addressable like a 422: errors[] points at the password field
    # and carries a machine-readable code in `type`.
    with pytest.raises(PasswordPolicyError) as exc_info:
        validate_password("password1234", policy=_SHIPPED)
    error = exc_info.value
    assert error.status_code == status.HTTP_400_BAD_REQUEST
    assert error.errors is not None
    assert error.errors[0].loc[-1] == "password"
    assert error.errors[0].type == "password_too_common"


@pytest.mark.unit
def test_password_policy_similarity_carries_its_own_code() -> None:
    with pytest.raises(PasswordPolicyError) as exc_info:
        validate_password("ada.lovelace@example.com", identity=["ada.lovelace@example.com"], policy=_SHIPPED)
    assert exc_info.value.errors is not None
    assert exc_info.value.errors[0].type == "password_too_similar"


@pytest.mark.unit
def test_shipped_policy_requires_no_character_classes() -> None:
    # Regression pin for the knobs' defaults: an all-lowercase, symbol-free passphrase that
    # passed before the policy became configurable must still pass.
    validate_password("correcthorsebatterystaple-xyz", policy=_SHIPPED)


@pytest.mark.unit
def test_configured_minimum_rejects_a_password_the_schema_floor_accepts() -> None:
    with pytest.raises(PasswordPolicyError) as exc_info:
        validate_password("short-12", policy=PasswordPolicy(min_length=16))
    assert exc_info.value.errors is not None
    assert exc_info.value.errors[0].type == "password_too_short"


@pytest.mark.unit
def test_configured_minimum_accepts_a_password_of_exactly_that_length() -> None:
    password = "x" * 7 + "-uncommon"
    # Asserted, not eyeballed: the point of the test is the length, and an edit to the literal
    # that misses the boundary turns an off-by-one in `_reject_too_short` into a green suite.
    assert len(password) == 16

    validate_password(password, policy=PasswordPolicy(min_length=16))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("policy", "value", "code"),
    [
        (PasswordPolicy(require_uppercase=True), "lower-case-only-42", "password_missing_uppercase"),
        (PasswordPolicy(require_digit=True), "no-digits-in-here", "password_missing_digit"),
        (PasswordPolicy(require_symbol=True), "noSymbolsInHere42", "password_missing_symbol"),
    ],
)
def test_character_class_requirements_carry_their_own_codes(policy: PasswordPolicy, value: str, code: str) -> None:
    with pytest.raises(PasswordPolicyError) as exc_info:
        validate_password(value, policy=policy)
    assert exc_info.value.errors is not None
    assert exc_info.value.errors[0].type == code


@pytest.mark.unit
def test_all_character_class_requirements_together_accept_a_conforming_password() -> None:
    policy = PasswordPolicy(require_uppercase=True, require_digit=True, require_symbol=True)
    validate_password("Uncommon-passphrase-42", policy=policy)


@pytest.mark.unit
def test_symbol_rule_accepts_a_non_ascii_symbol() -> None:
    # The rule is "not a letter and not a digit", so it can't quietly exclude non-ASCII layouts.
    validate_password("uncommon-passphrase§42", policy=PasswordPolicy(require_symbol=True))


@pytest.mark.unit
def test_length_is_reported_before_the_character_classes() -> None:
    # The cheapest, most actionable failure wins: a user retyping a short password shouldn't be
    # told about a missing symbol first and then about the length.
    policy = PasswordPolicy(min_length=20, require_symbol=True)
    with pytest.raises(PasswordPolicyError) as exc_info:
        validate_password("noSymbolsHere42", policy=policy)
    assert exc_info.value.errors is not None
    assert exc_info.value.errors[0].type == "password_too_short"


@pytest.mark.unit
def test_from_settings_carries_every_knob() -> None:
    settings = PlatformSettings(
        default_license_id=uuid4(),
        password_min_length=20,
        password_require_uppercase=True,
        password_require_digit=True,
        password_require_symbol=True,
    )

    policy = PasswordPolicy.from_settings(settings)

    assert policy == PasswordPolicy(min_length=20, require_uppercase=True, require_digit=True, require_symbol=True)


@pytest.mark.unit
def test_the_knob_default_tracks_the_schema_floor() -> None:
    """The stored default and the floor this module owns are one value, not two that agree today.

    Raising `MIN_PASSWORD_LENGTH` alone would give `PlatformSettingsUpdate` a `ge` above the
    default the model's DDL keeps writing — a stored value the schema itself would reject, with
    the configured-minimum rule dead behind a 422.
    """
    assert PlatformSettings.model_fields["password_min_length"].default == MIN_PASSWORD_LENGTH
    ddl_default = SQLModel.metadata.tables["platform_settings"].columns["password_min_length"].server_default
    assert isinstance(ddl_default, DefaultClause)
    assert str(ddl_default.arg) == str(MIN_PASSWORD_LENGTH)
