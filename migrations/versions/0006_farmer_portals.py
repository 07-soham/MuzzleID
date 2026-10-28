"""Add farmer accounts and explicit animal ownership for customer isolation."""
from alembic import op
import sqlalchemy as sa

revision = "0006_farmer_portals"
down_revision = "0005_encrypted_muzzle_id"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "farmers" not in tables:
        op.create_table(
        "farmers",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("full_name", sa.String(length=120), nullable=False),
        sa.Column("mobile", sa.String(length=32), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=True),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("session_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("mobile"),
        sa.UniqueConstraint("email"),
        )
    farmer_cols = {col["name"] for col in sa.inspect(bind).get_columns("farmers")}
    if "session_version" not in farmer_cols:
        with op.batch_alter_table("farmers") as batch:
            batch.add_column(sa.Column("session_version", sa.Integer(), server_default="0", nullable=False))
    for column in ("mobile", "email"):
        index_name = f"ix_farmers_{column}"
        if index_name not in {ix["name"] for ix in sa.inspect(bind).get_indexes("farmers")}:
            op.create_index(index_name, "farmers", [column])
    animals_cols = {col["name"] for col in sa.inspect(bind).get_columns("animals")}
    if "owner_id" not in animals_cols:
        with op.batch_alter_table("animals") as batch:
            batch.add_column(sa.Column("owner_id", sa.String(length=36), nullable=True))
            batch.create_foreign_key("fk_animals_owner_id_farmers", "farmers", ["owner_id"], ["id"], ondelete="SET NULL")
    if "ix_animals_owner_id" not in {ix["name"] for ix in sa.inspect(bind).get_indexes("animals")}:
        op.create_index("ix_animals_owner_id", "animals", ["owner_id"])
    verification_cols = {col["name"] for col in sa.inspect(bind).get_columns("verifications")}
    if "farmer_id" not in verification_cols:
        with op.batch_alter_table("verifications") as batch:
            batch.add_column(sa.Column("farmer_id", sa.String(length=36), nullable=True))
            batch.create_foreign_key("fk_verifications_farmer_id_farmers", "farmers", ["farmer_id"], ["id"], ondelete="SET NULL")
    if "ix_verifications_farmer_id" not in {ix["name"] for ix in sa.inspect(bind).get_indexes("verifications")}:
        op.create_index("ix_verifications_farmer_id", "verifications", ["farmer_id"])
    if "farmer_requests" not in set(sa.inspect(bind).get_table_names()):
        op.create_table(
        "farmer_requests",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("farmer_id", sa.String(length=36), nullable=False),
        sa.Column("animal_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("reference", sa.String(length=24), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["farmer_id"], ["farmers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["animal_id"], ["animals.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("reference"),
        )
    for column in ("farmer_id", "animal_id", "reference"):
        index_name = f"ix_farmer_requests_{column}"
        if index_name not in {ix["name"] for ix in sa.inspect(bind).get_indexes("farmer_requests")}:
            op.create_index(index_name, "farmer_requests", [column])
    if "farmer_request_events" not in set(sa.inspect(bind).get_table_names()):
        op.create_table(
        "farmer_request_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("previous_status", sa.String(length=24), nullable=True),
        sa.Column("new_status", sa.String(length=24), nullable=False),
        sa.Column("actor_role", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["request_id"], ["farmer_requests.id"], ondelete="CASCADE"),
        )
    if "ix_farmer_request_events_request_id" not in {ix["name"] for ix in sa.inspect(bind).get_indexes("farmer_request_events")}:
        op.create_index("ix_farmer_request_events_request_id", "farmer_request_events", ["request_id"])


def downgrade():
    op.drop_index("ix_farmer_request_events_request_id", table_name="farmer_request_events")
    op.drop_table("farmer_request_events")
    op.drop_index("ix_farmer_requests_reference", table_name="farmer_requests")
    op.drop_index("ix_farmer_requests_animal_id", table_name="farmer_requests")
    op.drop_index("ix_farmer_requests_farmer_id", table_name="farmer_requests")
    op.drop_table("farmer_requests")
    op.drop_index("ix_verifications_farmer_id", table_name="verifications")
    with op.batch_alter_table("verifications") as batch:
        batch.drop_constraint("fk_verifications_farmer_id_farmers", type_="foreignkey")
        batch.drop_column("farmer_id")
    op.drop_index("ix_animals_owner_id", table_name="animals")
    with op.batch_alter_table("animals") as batch:
        batch.drop_constraint("fk_animals_owner_id_farmers", type_="foreignkey")
        batch.drop_column("owner_id")
    op.drop_index("ix_farmers_email", table_name="farmers")
    op.drop_index("ix_farmers_mobile", table_name="farmers")
    with op.batch_alter_table("farmers") as batch:
        batch.drop_column("session_version")
    op.drop_table("farmers")
