---
tags: [component, conversations, security]
aliases: [content_crypto, seal_content, unseal_content, content_protected, content_encrypted, rewraptranscripts, CONVERSATION_SECRETS_KEY]
---

# Conversation content sealing (at rest)

Message text of a conversation created under a **data licence that protects conversation data** is stored encrypted. `Message.content` is therefore *sometimes* ciphertext — never read the column directly.

Module: `app/core/conversations/content_crypto.py`. Rotation sweep: `app/core/conversations/services/rewrap.py` (`make rewraptranscripts`).

> This shares **nothing** with [AI Gateway - key encryption](ai-gateway-key-encryption.md), which seals model API keys. Different format, different key, different rotation story. The separation is the point: the two datasets keep separate blast radii, and `Settings` refuses at boot if the two keyrings share a value.

## What decides whether a conversation is sealed

`DataLicense.protects_conversation_data` is a licence-level flag; the curated **`No license`** entry is the one that carries it today. At conversation *create* time the [effective licence](licenses.md#the-cascade-resolution-at-the-projection-layer) is resolved and frozen onto the row:

```python
# app/core/conversations/models.py — Conversation
# Resolved from the evaluation's effective licence when the conversation is created, and never
# re-derived: a licence changed afterwards does not reach conversations that already exist.
content_protected: bool = Field(default=False, ...)
```

Both create paths set it (the single create and the batch group-create). Message writes read `content_protected` instead of walking group → evaluation → licence on the streaming path.

The **per-row** discriminator is separate: `Message.content_encrypted`. A protected conversation still stores empty text as-is, so its `streaming` placeholders and error rows are unsealed while its real messages are sealed.

## Storage format

```
<kid>:v2:<base64(nonce ‖ ciphertext)>
```

AES-256-GCM, a 12-byte nonce, a one-byte hex **key-id** prefix and a format version. The GCM tag rides inside `ciphertext`, per the AESGCM convention. Associated data is the constant `b"messages.content"`, which binds a ciphertext to this column — a value pasted in from elsewhere fails to open rather than being served as transcript text.

The key-id is derived as a *second* SHA-256 of the cipher key, never the cipher key itself; deriving it from the first hash would publish a byte of the key on every stored row.

```python
def seal_content(plaintext: str, *, protected: bool, settings: Settings) -> tuple[str, bool]:
    if not protected or not plaintext:
        return plaintext, False
    ...
    return f"{kid}:{ENVELOPE}{base64.b64encode(nonce + sealed).decode()}", True
```

Both halves — the stored value and the `content_encrypted` flag — come back from **one** call, so the discriminator cannot disagree with the column.

`unseal_content(stored, *, encrypted, settings)` is keyed on the row's own flag. Failure modes are explicit `ContentDecryptError`s (wrong envelope, unknown key-id, bad base64, too short, authentication failed) rather than a library error surfacing as a 500.

## Every read path goes through `unseal_content`

Because the column is sometimes ciphertext, the *complete* list of readers matters:

| Reader | File |
|---|---|
| `MessageBase` / `TranscriptMessage` projections | `app/core/conversations/schemas.py` |
| `conversation_history` (the prompt sent to the model) | `app/core/conversations/services/messages.py` |
| the replay + `continue` paths | `app/api/v1/messages.py` |
| the shared export `message_dicts` and the `transcript` template row | `app/core/exports/conversation_messages.py` |
| the rotation sweep | `app/core/conversations/services/rewrap.py` |

**Only `messages.content` is sealed.** A conversation's `title`, its `tags`, a message's `tags` and the recorded `tag_context` are user-authored text that stays plaintext and rides the same exports.

## Keys and rotation

Two env slots, both `SecretStr`, minimum 32 chars:

| Setting | Meaning |
|---|---|
| `CONVERSATION_SECRETS_KEY` | **required** — a deployment that omits it fails at boot instead of sealing transcripts under the credential key |
| `CONVERSATION_SECRETS_KEY_RETIRED` | the outgoing key during a rotation window; rows tagged with its id keep opening |

Two startup validators back this: one refuses a key-id collision between the active and retired slot (1/256, but a mis-routed open is worse than a boot failure), the other refuses any overlap with `MODEL_SECRETS_KEY` and its retired slot.

Rotation is a **supervised deploy step**, deliberately not a Celery task:

```mermaid
flowchart LR
    A[promote new CONVERSATION_SECRETS_KEY] --> B[old value to _RETIRED]
    B --> C[restart every replica]
    C --> D[make rewraptranscripts]
    D --> E{remaining is zero}
    E -->|no| D
    E -->|yes| F[drop the retired key]
```

`make rewraptranscripts` re-wraps rows onto the active key and format; `ARGS=--dry-run` only counts what is left. The predicate matches on **format as well as key**, so a row written in a shape this build cannot read is *surfaced* — counted `unreadable`, logged with its id — rather than skipped silently. It is never moved: moving a row means opening it first.

> Restarting every replica before the sweep is not optional — a replica still holding the old active key would keep writing rows the sweep has already passed.

**Model credentials have no equivalent sweep.** Dropping a retired `MODEL_SECRETS_KEY` means re-entering every credential still sealed under it, by hand.

## Related

- [Conversations](conversations.md) — where `content_protected` is stamped
- [Message persistence (write path)](message-persistence-write-path.md) — the writer that seals
- [Message](../data-models/message.md) — `content` / `content_encrypted`
- [Data licensing & platform settings](licenses.md) — `protects_conversation_data` and the cascade that resolves it
- [AI Gateway - key encryption](ai-gateway-key-encryption.md) — the *other*, unrelated crypto mechanism
- [Configuration (Settings)](configuration-settings.md) — the two key slots and their validators
- [Exports](exports.md) — an export unseals through the same helper
