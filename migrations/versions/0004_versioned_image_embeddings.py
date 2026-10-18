"""Allow retaining embeddings for multiple model versions per image."""
import sqlalchemy as sa
from alembic import op

revision = "0004_versioned_embeddings"
down_revision = "0003_image_quality"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("image_embeddings"):
        return
    primary_key = sa.inspect(bind).get_pk_constraint("image_embeddings").get("constrained_columns") or []
    if set(primary_key) == {"image_id", "model_version"}:
        return
    op.create_table(
        "image_embeddings_new",
        sa.Column("image_id", sa.String(length=36), nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column("vector_encrypted", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["image_id"], ["animal_images.id"], name="fk_image_embeddings_image_id_animal_images", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("image_id", "model_version", name="pk_image_embeddings_new"),
    )
    op.execute(sa.text("INSERT INTO image_embeddings_new (image_id, model_version, vector_encrypted) SELECT image_id, model_version, vector_encrypted FROM image_embeddings"))
    op.drop_table("image_embeddings")
    op.rename_table("image_embeddings_new", "image_embeddings")
    op.create_index("ix_image_embeddings_model_version", "image_embeddings", ["model_version"])


def downgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("image_embeddings"):
        return
    duplicate = bind.execute(sa.text("SELECT image_id FROM image_embeddings GROUP BY image_id HAVING COUNT(*) > 1 LIMIT 1")).first()
    if duplicate:
        raise RuntimeError("Cannot downgrade: multiple model versions are stored for at least one image.")
    op.create_table(
        "image_embeddings_old",
        sa.Column("image_id", sa.String(length=36), nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column("vector_encrypted", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["image_id"], ["animal_images.id"], name="fk_image_embeddings_image_id_animal_images_old", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("image_id", name="pk_image_embeddings_old"),
    )
    op.execute(sa.text("INSERT INTO image_embeddings_old (image_id, model_version, vector_encrypted) SELECT image_id, model_version, vector_encrypted FROM image_embeddings"))
    op.drop_table("image_embeddings")
    op.rename_table("image_embeddings_old", "image_embeddings")
    op.create_index("ix_image_embeddings_model_version", "image_embeddings", ["model_version"])
