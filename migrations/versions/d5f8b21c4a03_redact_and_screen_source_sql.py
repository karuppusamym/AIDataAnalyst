"""Redact and screen source-supplied SQL at rest (INV-6, ADR-0013)

Envelope 1.1 introduced two new columns holding SQL text taken verbatim from a source:
`metadata_view_definition.definition_sql` and `metadata_routine.body_sql`. Both were stored
raw. Two problems followed, neither caught by the suite at the time.

**INV-6.** A SQL statement carries source values in its literals -- `WHERE ssn =
'123-45-6789'` is a source value written in a different syntax, so storing the statement
stores the value. The same codebase already treats persisted SQL correctly on the dbt path
(`compiled_sql_hash` + `compiled_sql_redacted`, raw artifact never kept); the newer path did
not. The INV-6 invariant test did not notice because it drives the query gateway only.

**ADR-0013.** Procedure bodies are source-controlled text that meaning inference and tool
generation are both designed to read, which makes them the largest indirect-injection
surface in the platform. The gap is recorded in four documents and was addressed in none.

This migration replaces the raw columns with redacted text plus a fingerprint, and adds a
write-time screening verdict.

**The raw columns are dropped, not migrated.** Any text already stored is exactly what has
been decided must not be stored, so carrying it forward would defeat the change. The
fingerprint column is populated on the next scan; rows keep their availability state and
are refreshed by normal re-ingestion.

Revision ID: d5f8b21c4a03
Revises: a1c9f4b7e230
Create Date: 2026-08-30 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5f8b21c4a03"
down_revision: str | None = "a1c9f4b7e230"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TARGETS = (
    ("metadata_view_definition", "definition_sql", "definition_sql_redacted",
     "definition_fingerprint", "availability_matches_definition"),
    ("metadata_routine", "body_sql", "body_sql_redacted",
     "body_fingerprint", "availability_matches_body"),
)


def upgrade() -> None:
    for table, raw_column, redacted_column, fingerprint_column, check_name in _TARGETS:
        # The CHECK ties availability to the stored column, so it has to be rebuilt
        # around the new one rather than left pointing at a column about to disappear.
        op.drop_constraint(check_name, table, type_="check")
        op.add_column(table, sa.Column(redacted_column, sa.Text(), nullable=True))
        op.add_column(table, sa.Column(fingerprint_column, sa.String(64), nullable=True))
        op.add_column(
            table,
            sa.Column("redaction_status", sa.String(20), nullable=False, server_default="PARSED"),
        )
        op.add_column(
            table,
            sa.Column("screening_status", sa.String(20), nullable=False, server_default="CLEAN"),
        )
        op.add_column(
            table,
            sa.Column("screening_reason_codes", sa.JSON(), nullable=False, server_default="[]"),
        )
        # Carry availability forward so the rebuilt CHECK holds for existing rows: a row
        # that had text keeps a non-NULL (empty) redacted value until it is re-scanned.
        op.execute(
            sa.text(
                f"UPDATE {table} SET {redacted_column} = '' "  # noqa: S608
                f"WHERE {raw_column} IS NOT NULL"
            )
        )
        op.drop_column(table, raw_column)
        op.create_check_constraint(
            check_name, table, f"(availability = 'AVAILABLE') = ({redacted_column} IS NOT NULL)"
        )


def downgrade() -> None:
    for table, raw_column, redacted_column, fingerprint_column, check_name in _TARGETS:
        op.drop_constraint(check_name, table, type_="check")
        op.add_column(table, sa.Column(raw_column, sa.Text(), nullable=True))
        op.execute(
            sa.text(
                f"UPDATE {table} SET {raw_column} = {redacted_column} "  # noqa: S608
                f"WHERE {redacted_column} IS NOT NULL"
            )
        )
        op.drop_column(table, "screening_reason_codes")
        op.drop_column(table, "screening_status")
        op.drop_column(table, "redaction_status")
        op.drop_column(table, fingerprint_column)
        op.drop_column(table, redacted_column)
        op.create_check_constraint(
            check_name, table, f"(availability = 'AVAILABLE') = ({raw_column} IS NOT NULL)"
        )
