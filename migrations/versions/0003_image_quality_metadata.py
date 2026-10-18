"""Persist transparent, non-decision image capture heuristics."""
import sqlalchemy as sa
from alembic import op

revision = "0003_image_quality"
down_revision = "0002_image_format"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("animal_images")}
    if "quality_json" not in columns:
        op.add_column("animal_images", sa.Column("quality_json", sa.Text(), nullable=False, server_default="{}"))


def downgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("animal_images")}
    if "quality_json" in columns:
        op.drop_column("animal_images", "quality_json")
