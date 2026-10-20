"""Add local material source, revision, and processing foundations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046_material_foundation"
down_revision: str | None = "0045_goal_error_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "material_roots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("display_name", sa.String(160), nullable=False),
        sa.Column("canonical_path", sa.Text(), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_material_roots_status", "material_roots", ["status"])

    op.create_table(
        "material_sources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(24), nullable=False, server_default="LOCAL_FILE"),
        sa.Column("root_id", sa.String(36), nullable=True),
        sa.Column("relative_path", sa.Text(), nullable=True),
        sa.Column("canonical_uri", sa.Text(), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("mime_type", sa.String(160), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["root_id"], ["material_roots.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("root_id", "relative_path", name="uq_material_source_local_path"),
    )
    op.create_index("ix_material_sources_kind", "material_sources", ["kind"])
    op.create_index("ix_material_sources_root_id", "material_sources", ["root_id"])
    op.create_index("ix_material_sources_status", "material_sources", ["status"])

    op.create_table(
        "material_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parser_name", sa.String(64), nullable=False, server_default="UNASSIGNED"),
        sa.Column("parser_version", sa.String(40), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="QUEUED"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["source_id"], ["material_sources.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("source_id", "revision", name="uq_material_revision_number"),
    )
    op.create_index("ix_material_revisions_source_id", "material_revisions", ["source_id"])
    op.create_index("ix_material_revisions_content_hash", "material_revisions", ["content_hash"])
    op.create_index("ix_material_revisions_status", "material_revisions", ["status"])

    op.create_table(
        "material_processing_units",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("unit_type", sa.String(24), nullable=False),
        sa.Column("range_start", sa.Integer(), nullable=True),
        sa.Column("range_end", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="QUEUED"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checkpoint_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("result_artifact_path", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["revision_id"], ["material_revisions.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "revision_id", "sequence", name="uq_material_processing_unit_sequence"
        ),
    )
    op.create_index(
        "ix_material_processing_units_revision_id",
        "material_processing_units",
        ["revision_id"],
    )
    op.create_index(
        "ix_material_processing_units_status", "material_processing_units", ["status"]
    )

    op.create_table(
        "goal_material_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("goal_id", sa.String(36), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("scope", sa.String(24), nullable=False, server_default="ALL"),
        sa.Column("usage_policy", sa.String(24), nullable=False, server_default="AUTO"),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["goal_id"], ["goals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["material_sources.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("goal_id", "source_id", name="uq_goal_material_link"),
    )
    op.create_index("ix_goal_material_links_goal_id", "goal_material_links", ["goal_id"])
    op.create_index("ix_goal_material_links_source_id", "goal_material_links", ["source_id"])
    op.create_index("ix_goal_material_links_status", "goal_material_links", ["status"])


def downgrade() -> None:
    op.drop_table("goal_material_links")
    op.drop_table("material_processing_units")
    op.drop_table("material_revisions")
    op.drop_table("material_sources")
    op.drop_table("material_roots")
