"""Baseline existing MuzzleID schema and create missing application tables."""
from alembic import op

from src.database.models import Base

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # The pre-migration MVP used metadata.create_all. create_all(checkfirst=True)
    # upgrades those existing databases without dropping user records and
    # creates the full current baseline for fresh deployments.
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade():
    # A baseline may represent pre-existing user data; never drop it implicitly.
    pass
