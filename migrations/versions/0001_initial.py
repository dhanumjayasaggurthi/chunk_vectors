"""Initial baseline of the enterprise data model.

Revision ID: 0001_initial
Revises:
"""
from alembic import op
from backend import store,enterprise_state,bulk  # noqa: ensure all models registered
revision='0001_initial'
down_revision=None
branch_labels=None
depends_on=None

def upgrade():
    store.Base.metadata.create_all(bind=op.get_bind())

def downgrade():
    # Destructive: never run in production without backup/change approval.
    store.Base.metadata.drop_all(bind=op.get_bind())
