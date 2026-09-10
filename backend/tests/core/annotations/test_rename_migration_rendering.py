"""The rename migration's audit rewrite must survive offline SQL rendering (`alembic upgrade --sql`).

The why lives on `_AUDIT_ACTION_TO_NOTE` in the migration itself: a *bound* pattern renders with a
doubled backslash under `literal_binds=True` and then matches nothing, so `--sql` emitted the DDL and
silently skipped the rewrite — invisible online, hence only a rendering-layer test can pin it.
Marked `unit` (no DB), located here because the migration's other tests are.
"""

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql

pytestmark = pytest.mark.unit

_REVISION = "d3f7b1c2a904"


def _rendered_offline(name: str) -> str:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    statement = getattr(script.get_revision(_REVISION).module, name)
    # `paramstyle="named"` matches `alembic/env.py`'s offline configure; the default pyformat
    # doubles any `%` in the rendered text, which would misrepresent a LIKE-bearing statement.
    compiled = statement.compile(dialect=postgresql.dialect(paramstyle="named"), compile_kwargs={"literal_binds": True})
    return str(compiled)


@pytest.mark.parametrize(
    ("name", "pattern"),
    [
        ("_AUDIT_ACTION_TO_NOTE", r"'^annotation\.'"),
        ("_AUDIT_ACTION_TO_ANNOTATION", r"'^note\.'"),
    ],
)
def test_audit_rewrite_renders_one_backslash_offline(name: str, pattern: str) -> None:
    rendered = _rendered_offline(name)

    # Fails if the pattern is ever bound again: the compiler would double the backslash, and PG
    # would match nothing against `\\.`.
    assert pattern in rendered
    assert "\\\\" not in rendered
