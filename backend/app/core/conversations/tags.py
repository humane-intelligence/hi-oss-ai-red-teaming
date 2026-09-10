"""Render a conversation's free-form tags into model-facing prompt context.

Tags are rendered under an instruction preamble so the model treats them as context to apply,
not stray text. The rendered block is handed to the gateway out-of-band (`dispatch_*`'s
``system_suffix``), which appends it to the system message — deliberately *not* merged into the
inference params, so a model with ``advanced_params_disabled`` still receives its tag context.
Keys render in sorted order so the composed prompt is deterministic.

Both halves of a tag are sanitised here — control chars and invisible
format chars are stripped and whitespace runs (incl. newlines/tabs) collapse to a single space —
so neither a value nor a key can forge an extra ``key: value`` line or a spoofed preamble. Keys
are additionally constrained at the schema boundary by ``is_valid_tag_key``, and the write edge
stores the sanitised form (``ConversationTags``), so stored == returned == sent. The pass here is
the prompt boundary's own layer: it re-cleans a value that reached storage by another route (a
direct DB write, an import, a pre-normalisation row). It bounds charset and shape only — not size;
the caps live at the schema boundary.
"""

import re

from app.core.helpers import sanitise_single_line

# Instruction preamble prepended to the rendered key/value lines so a model is nudged to apply
# the tags rather than ignore a bare list. Exposed so tests assert against one source of truth.
TAG_CONTEXT_PREAMBLE = "Context tags for this request — take them into account when responding:"
# Unanchored, because it is also published as the JSON Schema `propertyNames.pattern` for a tag map
# and that dialect matches anywhere in the string — the publishing side anchors it with `^…$`.
TAG_KEY_PATTERN = r"[A-Za-z0-9_.-]{1,64}"
# `\Z`, not `$`: `$` also matches before a trailing newline, and such a key would render as its own
# prompt line. The anchors are what enforce the rule, so a plain `.match()` is enough. The pattern is
# grouped because it is interpolated into two anchorings (here and `^…$` on the publishing side) —
# a top-level alternation added to it later would otherwise escape one of them.
TAG_KEY_RE = re.compile(rf"\A(?:{TAG_KEY_PATTERN})\Z")


def is_valid_tag_key(key: str) -> bool:
    """Whether `key` is usable as a tag key anywhere the API accepts one.

    Charset and length come from `TAG_KEY_PATTERN`, which is also published as the tag map's
    `propertyNames.pattern`. Dot-only keys (`.`, `..`) are rejected on top of it, here rather than in
    the pattern, so the published form needs no look-around — JSON Schema validators built on Rust
    `regex` or RE2 have none.
    """
    return TAG_KEY_RE.match(key) is not None and key.strip(".") != ""


def sent_tag_context(tags: dict[str, str]) -> dict[str, str]:
    """The sanitised pairs the rendered block carries — empty when nothing is sent.

    Iterates `tags` sorted by the raw (unsanitised) key, then sanitises each key/value pair — so the
    order is decided before sanitising, and the emitted key (which sanitising can change) is not
    necessarily the one it was sorted on. Tags whose value is empty (blank / whitespace /
    control-chars-only) are skipped — an unfilled tag carries no context, so it isn't sent to the
    model.
    """
    sent: dict[str, str] = {}
    for key in sorted(tags):
        value = sanitise_single_line(str(tags[key]))
        if not value:
            continue
        sent[sanitise_single_line(key)] = value
    return sent


def _render_pairs(sent: dict[str, str]) -> str:
    """Format already-sanitised pairs under the instruction preamble, in the order given."""
    return "\n".join([TAG_CONTEXT_PREAMBLE, *(f"{key}: {value}" for key, value in sent.items())])


def fold_tag_context(tags: dict[str, str]) -> tuple[dict[str, str], str | None]:
    """The pairs a prompt will carry and the block rendering them, sanitised once.

    A caller that needs both (the write path records the map and folds the block) must not derive
    them separately: the second derivation sorts on the sanitised key, so two lines can swap where
    sanitising changes a key's rank. Returns an empty map and ``None`` when nothing is sent.
    """
    sent = sent_tag_context(tags)
    return sent, _render_pairs(sent) if sent else None


def render_tag_context(tags: dict[str, str]) -> str | None:
    """Render tags under the instruction preamble (sorted, injection-safe), or ``None`` when nothing to send.

    Formats `sent_tag_context` rather than re-deriving it, so what a reply records and what its prompt
    carried cannot drift apart.
    """
    return fold_tag_context(tags)[1]
