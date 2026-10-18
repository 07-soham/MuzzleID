"""Store each accepted image's decoded format for authenticated previews."""
import sqlalchemy as sa
from alembic import op

revision = "0002_image_format"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("animal_images")}
    if "image_format" not in columns:
        op.add_column("animal_images", sa.Column("image_format", sa.String(length=8), nullable=False, server_default="JPEG"))


def downgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("animal_images")}
    if "image_format" in columns:
        op.drop_column("animal_images", "image_format")
