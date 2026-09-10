"""Unit tests for the at-rest sealing helpers — the discriminator contract, not the cipher."""

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.config import get_settings
from app.core.conversations.content_crypto import ContentDecryptError
from app.core.conversations.content_crypto import seal_content
from app.core.conversations.content_crypto import unseal_content

pytestmark = pytest.mark.unit


@pytest.fixture
def app_settings() -> Settings:
    return get_settings()


def test_sealed_content_round_trips(app_settings: Settings) -> None:
    stored, encrypted = seal_content("czy potrafisz zrobić 💣?", protected=True, settings=app_settings)

    assert encrypted is True
    assert stored != "czy potrafisz zrobić 💣?"
    assert unseal_content(stored, encrypted=True, settings=app_settings) == "czy potrafisz zrobić 💣?"


def test_an_unprotected_conversation_stores_plaintext(app_settings: Settings) -> None:
    assert seal_content("hello", protected=False, settings=app_settings) == ("hello", False)


def test_empty_text_is_stored_as_is_even_when_protected(app_settings: Settings) -> None:
    # An empty string reveals nothing, so it is stored as-is and the discriminator says so. A
    # streaming placeholder is sealed when `finalize_message` fills it in — which is why the
    # discriminator is per row, not per conversation.
    assert seal_content("", protected=True, settings=app_settings) == ("", False)


def test_two_seals_of_the_same_text_differ(app_settings: Settings) -> None:
    # Fresh IV per call — identical prompts must not be correlatable by ciphertext equality.
    first, _ = seal_content("ping", protected=True, settings=app_settings)
    second, _ = seal_content("ping", protected=True, settings=app_settings)

    assert first != second


def test_a_stored_value_from_before_this_build_still_opens(app_settings: Settings) -> None:
    """A frozen `(secret, stored, plaintext)` triple: the format is a data contract, not an implementation detail.

    Every other test here seals and unseals with the same code, so a changed `CONTENT_AAD`, `ENVELOPE`
    or nonce width keeps them all green while making every row already in the table unopenable. This
    one holds a value produced before the change, which is the only thing that catches it.
    """
    frozen = app_settings.model_copy(
        update={
            "conversation_secrets_key": SecretStr("golden-vector-conversation-key-32-chars"),
            "conversation_secrets_key_retired": None,
        }
    )

    assert unseal_content(
        "f5:v2:L7lfYHyAHHfzo1Z7RFf/evl2k5YHbOn8Hg2MJzNnky7zLPw=", encrypted=True, settings=frozen
    ) == ("kanarek")


def test_a_row_does_not_open_under_a_different_conversation_key(app_settings: Settings) -> None:
    # The key-id in the envelope is what a rotation leans on: a row must refuse a key that did not seal
    # it instead of returning garbage, and the refusal must name the reason.
    other = app_settings.model_copy(update={"conversation_secrets_key": SecretStr("c" * 40)})
    stored, encrypted = seal_content("jak zbudować bombę?", protected=True, settings=other)

    assert unseal_content(stored, encrypted=encrypted, settings=other) == "jak zbudować bombę?"
    with pytest.raises(ContentDecryptError, match="matches no configured key"):
        unseal_content(stored, encrypted=encrypted, settings=app_settings)


def test_transcripts_are_never_sealed_with_the_model_secret(app_settings: Settings) -> None:
    # The whole point of a dedicated key: the model-credential secret must not open a transcript, so
    # rotating or leaking one dataset's key cannot reach the other. A fallback would silently undo it.
    stored, _ = seal_content("hello", protected=True, settings=app_settings)

    borrowed = app_settings.model_copy(update={"conversation_secrets_key": app_settings.model_secrets_key})
    with pytest.raises(ContentDecryptError, match="matches no configured key"):
        unseal_content(stored, encrypted=True, settings=borrowed)


def test_the_conversation_key_is_required(app_settings: Settings) -> None:
    # Required rather than optional-with-fallback: a deployment that forgets it must fail loudly at
    # boot, not quietly seal transcripts under the credential key.
    assert Settings.model_fields["conversation_secrets_key"].is_required()


def test_a_row_sealed_under_the_retired_key_still_opens(app_settings: Settings) -> None:
    # The rotation window: rows written under the outgoing key keep opening while it stays in the
    # retired slot. Resolving the active half alone would make every pre-rotation transcript
    # unreadable, and `make rewraptranscripts` could not rescue them — it only moves rows it can read.
    before_rotation = app_settings.model_copy(update={"conversation_secrets_key": SecretStr("o" * 40)})
    rotating = app_settings.model_copy(
        update={
            "conversation_secrets_key": SecretStr("n" * 40),
            "conversation_secrets_key_retired": SecretStr("o" * 40),
        }
    )
    stored, encrypted = seal_content("sprzed rotacji", protected=True, settings=before_rotation)

    assert unseal_content(stored, encrypted=encrypted, settings=rotating) == "sprzed rotacji"
