"""merge AU-8 ORM-drift head with the SM-4/PG-4/OB-7 merge head

Pre-existing gap found while adding DQ-4's migration: this branch tip
(4beb9f2) already carried two divergent Alembic heads (09be3ab5b008 and
626211c0e077), violating ST-02's single-head CI gate. Not something this
change introduced -- confirmed via ``alembic heads`` against the unmodified
checkout before any DQ-4 model/migration was added. Reconciled here (a pure
merge point, no schema change) so DQ-4's own migration has one clean parent
instead of adding a third head.

Revision ID: 933ad6ad0731
Revises: 09be3ab5b008, 626211c0e077
Create Date: 2026-08-31 00:00:00
"""
from collections.abc import Sequence

revision: str = "933ad6ad0731"
down_revision: str | Sequence[str] | None = ("09be3ab5b008", "626211c0e077")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
