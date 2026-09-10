"""Small, dependency-free utilities shared across the codebase.

Keep this module tight — anything that grows its own surface (helpers,
config, tests) belongs in a dedicated module, not here.
"""

import unicodedata
from datetime import UTC
from datetime import datetime


def ensure_utc(value: datetime | None) -> datetime | None:
    """Normalize a datetime filter bound to UTC: a naive value is assumed UTC, an aware one converted.

    Idempotent, so it is safe as a field-level `AfterValidator` even though FastAPI's `Depends()`
    re-validates a filter model on construction — unlike `escape_like`, which must run once at the
    model level to avoid double-escaping. Keeps date-range filters honest about the "UTC" they document
    (a naive bound would otherwise be compared against a `timestamptz` column in the DB session's zone).
    """
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def escape_like(value: str) -> str:
    """Backslash-escape `\\`, `%`, `_` so the value matches literally inside a `LIKE`/`ILIKE` pattern.

    Pair with `column.ilike(pattern, escape="\\\\")` on the SQLAlchemy side
    so Postgres interprets the backslash as the escape character.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Invisible enough to disappear from a rendered chip or badge, or structural enough to forge a
# prompt line. `Cn` (unassigned) is excluded on purpose — see `sanitise_single_line`.
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs"})


def sanitise_single_line(text: str) -> str:
    """Strip control/invisible chars and collapse whitespace runs, so `text` stays one visible line.

    The shared invariant: what is stored, returned and rendered is the same string, and it occupies
    one line. Three callers rely on it — conversation tags, where a value also reaches a prompt,
    AI-model labels, which only ever reach a badge, and ad-hoc annotation labels, which reach a
    chip and a dedup key folded by the database.

    Drops the categories that can hide from a chip or, for the tag caller, forge prompt structure — `Cc`
    (control, incl. the whole C1 block), `Cf` (format: soft hyphen, BOM, arabic letter mark, the
    bidi isolates and overrides), `Co` (private use) and `Cs` (surrogates) — except the whitespace
    ones, which `.split()` folds instead. `Cn` (unassigned) is deliberately **not** dropped: it is a
    property of the interpreter's UCD, not of the text, so codepoints assigned in a newer Unicode
    than this build (`U+323B0`, `U+1FADD` under UCD 16) would silently vanish from the
    rendered text while the chip still shows them — the exact divergence this stripping exists to prevent.

    Consequence: the unassigned holes of the tag plane (`U+E0000`, `U+E0002`-`U+E001F`) now pass,
    where a code-point range used to catch them. They render as tofu rather than as nothing, and
    cannot forge a ``key: value`` line, so this is the cheaper side of the trade — dropping real
    text is worse than passing a visible replacement glyph. The plane's *assigned* members are `Cf`
    and still go.
    """
    kept = "".join(c for c in text if c.isspace() or unicodedata.category(c) not in _INVISIBLE_CATEGORIES)
    return " ".join(kept.split())
