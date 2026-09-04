"""NT-1: allow a notification that is not about a quality incident

Revision ID: c4a8d2f61e93
Revises: b7e3f19d5c24
Create Date: 2026-09-04 20:00:00.000000

`notification_event` was built for DQ-1, where every notification is about an
incident matched by a rule, so both foreign keys were NOT NULL. NT-1 delivers
governance events -- an approval request, a kill switch, a certification about
to lapse -- which have neither.

Making the two columns nullable is the smaller change than a second, nearly
identical delivery ledger: an operator asking "was this notification
delivered" should have one table to look in. A row with a NULL `incident_id`
is a governance notification; a row with one is DQ-1's, and every existing row
keeps its meaning untouched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4a8d2f61e93"
down_revision: str | Sequence[str] | None = "b7e3f19d5c24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("notification_event") as batch:
        batch.alter_column("incident_id", existing_type=sa.Uuid(), nullable=True)
        batch.alter_column("rule_id", existing_type=sa.Uuid(), nullable=True)


def downgrade() -> None:
    # Governance notifications carry no incident, so they must go before the
    # column can be NOT NULL again -- otherwise the constraint cannot be
    # restored on any database that has used NT-1.
    op.execute("DELETE FROM notification_event WHERE incident_id IS NULL")
    with op.batch_alter_table("notification_event") as batch:
        batch.alter_column("incident_id", existing_type=sa.Uuid(), nullable=False)
        batch.alter_column("rule_id", existing_type=sa.Uuid(), nullable=False)
