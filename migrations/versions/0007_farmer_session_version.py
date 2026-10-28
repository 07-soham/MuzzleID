"""Ensure existing farmer tables support revocable login sessions."""
from alembic import op
import sqlalchemy as sa


revision = "0007_farmer_session_version"
down_revision = "0006_farmer_portals"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("farmers")}
    if "session_version" not in columns:
        with op.batch_alter_table("farmers") as batch:
            batch.add_column(sa.Column("session_version", sa.Integer(), server_default="0", nullable=False))


def downgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("farmers")}
    if "session_version" in columns:
        with op.batch_alter_table("farmers") as batch:
            batch.drop_column("session_version")
