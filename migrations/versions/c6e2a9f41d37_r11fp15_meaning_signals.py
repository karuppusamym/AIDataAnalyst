"""R11-FP15: meaning signals for descriptions, semantic models and glossary terms

Revision ID: c6e2a9f41d37
Revises: a5d1c8e3f7b2
Create Date: 2026-09-18

`metadata_change_signal` recorded source changes (definition, structure, permission) and one kind
of meaning change, an ontology version published. The rest of the meaning a reader is given moved
without a record: an approved table, column or routine description superseded or withdrawn, a
semantic model or glossary term version superseded. `aida.change_signals.record_meaning_signals`
now records those as `MEANING_RETIRED`, with the retired version as the subject and the class
saying whether another approved version replaced it (`MEANING_REPLACED`) or none did
(`MEANING_WITHDRAWN`).

The three check constraints widen to admit the five subject kinds, the signal type and the two
classes. Nothing is backfilled here: the sweep records what it finds on its first pass, once, and
the anti-join on `(subject_kind, subject_id)` -- already indexed by
`ix_metadata_change_signal_subject` -- is what makes it once.

`downgrade` restores the narrower checks and **refuses** while any widened row exists, rather than
deleting a recorded change to make room for an older schema: a meaning signal is evidence that the
meaning a context product stood on moved, and that evidence is not the downgrade's to discard.
Delete those rows deliberately first if a downgrade is really wanted.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6e2a9f41d37"
down_revision: str | Sequence[str] | None = "a5d1c8e3f7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "metadata_change_signal"
_SUBJECT_KIND_CHECK = "ck_metadata_change_signal_subject_kind"
_SIGNAL_TYPE_CHECK = "ck_metadata_change_signal_signal_type"
_CHANGE_CLASS_CHECK = "ck_metadata_change_signal_change_class"

_OLD_SUBJECT_KINDS = "subject_kind IN ('TABLE', 'VIEW', 'ROUTINE', 'GRANT', 'ONTOLOGY')"
_NEW_SUBJECT_KINDS = (
    "subject_kind IN ('TABLE', 'VIEW', 'ROUTINE', 'GRANT', 'ONTOLOGY', "
    "'TABLE_DESCRIPTION', 'COLUMN_DESCRIPTION', 'ROUTINE_DESCRIPTION', "
    "'SEMANTIC_MODEL', 'GLOSSARY_TERM')"
)
_OLD_SIGNAL_TYPES = (
    "signal_type IN ('DEFINITION_CHANGED', 'STRUCTURE_CHANGED', 'DEPRECATED', "
    "'REACTIVATED', 'PERMISSION_CHANGED', 'MEANING_PUBLISHED')"
)
_NEW_SIGNAL_TYPES = (
    "signal_type IN ('DEFINITION_CHANGED', 'STRUCTURE_CHANGED', 'DEPRECATED', "
    "'REACTIVATED', 'PERMISSION_CHANGED', 'MEANING_PUBLISHED', 'MEANING_RETIRED')"
)
_OLD_CLASSES = (
    "change_class IS NULL OR change_class IN ('LITERAL_ONLY', 'STRUCTURAL', "
    "'GRANT_ADDED', 'GRANT_MODIFIED', 'GRANT_REVOKED', 'SIGNATURE_CHANGED', "
    "'COLUMNS_ADDED', 'COLUMNS_RETURNED', 'COLUMNS_REMOVED', 'COLUMNS_RETYPED')"
)
_NEW_CLASSES = (
    "change_class IS NULL OR change_class IN ('LITERAL_ONLY', 'STRUCTURAL', "
    "'GRANT_ADDED', 'GRANT_MODIFIED', 'GRANT_REVOKED', 'SIGNATURE_CHANGED', "
    "'COLUMNS_ADDED', 'COLUMNS_RETURNED', 'COLUMNS_REMOVED', 'COLUMNS_RETYPED', "
    "'MEANING_REPLACED', 'MEANING_WITHDRAWN')"
)

#: Every row only the widened constraints admit. `MEANING_RETIRED` is the only type these kinds
#: and classes are ever written with, but each is named so a hand-written row is caught too.
_WIDENED_ROWS = (
    "SELECT count(*) FROM metadata_change_signal WHERE subject_kind IN ("
    "'TABLE_DESCRIPTION', 'COLUMN_DESCRIPTION', 'ROUTINE_DESCRIPTION', 'SEMANTIC_MODEL', "
    "'GLOSSARY_TERM') OR signal_type = 'MEANING_RETIRED' "
    "OR change_class IN ('MEANING_REPLACED', 'MEANING_WITHDRAWN')"
)


def _replace(name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), _TABLE, type_="check")
    op.create_check_constraint(op.f(name), _TABLE, condition)


def upgrade() -> None:
    _replace(_SUBJECT_KIND_CHECK, _NEW_SUBJECT_KINDS)
    _replace(_SIGNAL_TYPE_CHECK, _NEW_SIGNAL_TYPES)
    _replace(_CHANGE_CLASS_CHECK, _NEW_CLASSES)


def downgrade() -> None:
    widened = op.get_bind().execute(sa.text(_WIDENED_ROWS)).scalar_one()
    if widened:
        raise RuntimeError(
            f"metadata_change_signal holds {widened} meaning signal(s) the pre-R11-FP15 schema "
            "cannot represent. Refusing to downgrade rather than delete recorded changes; "
            "remove them deliberately first if the downgrade is intended."
        )
    _replace(_CHANGE_CLASS_CHECK, _OLD_CLASSES)
    _replace(_SIGNAL_TYPE_CHECK, _OLD_SIGNAL_TYPES)
    _replace(_SUBJECT_KIND_CHECK, _OLD_SUBJECT_KINDS)
