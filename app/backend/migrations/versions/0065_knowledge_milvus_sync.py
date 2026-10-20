"""Track synchronization of knowledge vectors to an external vector store."""
from alembic import op
import sqlalchemy as sa

revision = "0065_knowledge_milvus_sync"
down_revision = "0064_shared_knowledge"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("knowledge_chunks", sa.Column("milvus_status", sa.String(16), nullable=False,
        server_default="PENDING"))
    op.add_column("knowledge_chunks", sa.Column("milvus_attempts", sa.Integer(), nullable=False,
        server_default="0"))
    op.add_column("knowledge_chunks", sa.Column("milvus_target", sa.String(64), nullable=False,
        server_default=""))
    op.add_column("knowledge_chunks", sa.Column("milvus_lease_id", sa.String(36)))
    op.add_column("knowledge_chunks", sa.Column("milvus_lease_until", sa.DateTime(timezone=True)))
    op.create_index("ix_knowledge_chunks_milvus_status", "knowledge_chunks", ["milvus_status"])


def downgrade():
    op.drop_index("ix_knowledge_chunks_milvus_status", table_name="knowledge_chunks")
    op.drop_column("knowledge_chunks", "milvus_lease_until")
    op.drop_column("knowledge_chunks", "milvus_lease_id")
    op.drop_column("knowledge_chunks", "milvus_target")
    op.drop_column("knowledge_chunks", "milvus_attempts")
    op.drop_column("knowledge_chunks", "milvus_status")
