"""Persist approved RIMdocs metadata fields.

Revision ID: 0002_birth_date_metadata
Revises: 0001_initial
"""
from alembic import op
import sqlalchemy as sa

revision = '0002_birth_date_metadata'
down_revision = '0001_initial'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('documents', sa.Column('edms_number', sa.String(128), nullable=True))
    op.add_column('documents', sa.Column('product_code', sa.String(255), nullable=True))
    op.add_column('documents', sa.Column('document_domain', sa.String(128), nullable=True))
    op.add_column('documents', sa.Column('document_component', sa.String(255), nullable=True))

def downgrade():
    op.drop_column('documents', 'document_component')
    op.drop_column('documents', 'document_domain')
    op.drop_column('documents', 'product_code')
    op.drop_column('documents', 'edms_number')
