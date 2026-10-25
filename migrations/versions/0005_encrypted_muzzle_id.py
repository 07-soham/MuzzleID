"""Add encrypted muzzle_id column to animals table."""
import sqlalchemy as sa
from alembic import op

revision = "0005_encrypted_muzzle_id"
down_revision = "0004_versioned_embeddings"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("animals"):
        return
    cols = [c["name"] for c in insp.get_columns("animals")]
    if "muzzle_id" not in cols:
        op.add_column("animals", sa.Column("muzzle_id", sa.String(length=64), server_default="", nullable=False))
        op.create_index("ix_animals_muzzle_id", "animals", ["muzzle_id"])
        # Backfill existing animals with persistent muzzle IDs if any exist
        try:
            from src.security.crypto import encrypted_muzzle_id, data_key_bytes
            from src.config import get_settings
            key = data_key_bytes(get_settings().get("data_key", ""))
            rows = bind.execute(sa.text("SELECT id FROM animals")).fetchall()
            for (aid,) in rows:
                mid = encrypted_muzzle_id(aid, key)
                bind.execute(sa.text("UPDATE animals SET muzzle_id = :mid WHERE id = :aid"), {"mid": mid, "aid": aid})
        except Exception:
            pass


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("animals"):
        return
    cols = [c["name"] for c in insp.get_columns("animals")]
    if "muzzle_id" in cols:
        op.drop_index("ix_animals_muzzle_id", table_name="animals")
        op.drop_column("animals", "muzzle_id")
