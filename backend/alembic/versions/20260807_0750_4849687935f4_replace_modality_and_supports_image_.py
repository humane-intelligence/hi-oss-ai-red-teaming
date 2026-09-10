"""replace modality and supports_image_input with modality arrays

Revision ID: 4849687935f4
Revises: 5195eaa359f1
Create Date: 2026-08-07 07:50:45.636915
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "4849687935f4"
down_revision: str | None = "5195eaa359f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Module constants rather than inline literals so the tests can run them against seeded
# rows — the migration itself only ever executes over empty tables under test.
_UPGRADE_BACKFILL = sa.text(
    """
    UPDATE ai_models
    SET input_modalities = CASE
            WHEN supports_image_input THEN ARRAY['text', 'image']
            ELSE ARRAY['text']
        END,
        output_modalities = CASE
            WHEN modality::text = 'text_to_image' THEN ARRAY['image']
            ELSE ARRAY['text']
        END
    """
)

# Lossy: a row producing both text and image collapses to text_to_text, since the
# old enum could only hold one output shape.
_DOWNGRADE_BACKFILL = sa.text(
    """
    UPDATE ai_models
    SET supports_image_input = ('image' = ANY(input_modalities)),
        modality = (CASE
            WHEN 'text' = ANY(output_modalities) THEN 'text_to_text'
            ELSE 'text_to_image'
        END)::modelmodality
    """
)


def upgrade() -> None:
    op.add_column(
        "ai_models",
        sa.Column("input_modalities", sa.ARRAY(sa.String()), server_default=sa.text("'{text}'"), nullable=False),
    )
    op.add_column(
        "ai_models",
        sa.Column("output_modalities", sa.ARRAY(sa.String()), server_default=sa.text("'{text}'"), nullable=False),
    )
    op.execute(_UPGRADE_BACKFILL)
    op.drop_column("ai_models", "supports_image_input")
    op.drop_column("ai_models", "modality")
    op.execute("DROP TYPE modelmodality")


def downgrade() -> None:
    op.execute("CREATE TYPE modelmodality AS ENUM ('text_to_text', 'text_to_image')")
    # Both columns land with a server_default the original schema didn't have, so the
    # NOT NULL add succeeds on a populated table; the defaults come off after the backfill.
    op.add_column(
        "ai_models",
        sa.Column(
            "modality",
            postgresql.ENUM("text_to_text", "text_to_image", name="modelmodality", create_type=False),
            server_default="text_to_text",
            nullable=False,
        ),
    )
    op.add_column(
        "ai_models",
        sa.Column("supports_image_input", sa.BOOLEAN(), server_default=sa.text("false"), nullable=False),
    )
    op.execute(_DOWNGRADE_BACKFILL)
    op.alter_column("ai_models", "modality", server_default=None)
    op.drop_column("ai_models", "output_modalities")
    op.drop_column("ai_models", "input_modalities")
