"""RL-5: naming and type normalization for cross-source relationship inference.

`discover_cross_source_relationship_candidates` (intelligence_api.py) matches a
column against a primary key in *another* datasource. Before this module, that
match was `source.name.lower() == target.name.lower()` and
`source.physical_type.lower() != target.physical_type.lower()` -- exact string
equality after lowercasing only. That survives nothing about a real
heterogeneous estate:

* Naming convention differs by team and by connector: `customer_id` (snake_case)
  vs. `customerId` (camelCase) vs. `CustomerId` (PascalCase) vs. `CUSTOMER_ID`
  (SCREAMING_CASE) are, to a human reviewer, obviously the same key -- but
  `.lower()` alone only collapses the last of those four into the first.
* Physical type spelling differs by dialect for the same logical type:
  Oracle `NUMBER(38,0)`, BigQuery `INT64`, Postgres `integer` and Snowflake
  `NUMBER` are all integer-family columns, but none of their lowercased
  strings are equal to each other.

Both functions below are pure, metadata-only (ADR-0014 -- no source values
touched or needed) and deliberately narrow: they normalize a name or a type
string into a comparable form, nothing more. They do not decide confidence,
do not look at cardinality, and do not replace the exact-match evidence
signal -- `discover_cross_source_relationship_candidates` still records
whether a match was exact or only canonical/family-level, so the reviewer
sees the difference (module 06 SS7: "a confidence number without its
reasoning is not reviewable").
"""

import re

# Splits a name into "word" chunks at:
#   - a lowercase/digit -> uppercase boundary (camelCase / PascalCase), and
#   - any run of underscores, hyphens, or whitespace (snake_case, kebab-case,
#     SCREAMING_SNAKE_CASE, "spaced words").
# A pure regex split -- no dictionary, no language model, so it stays
# metadata-only and dialect-agnostic by construction.
_WORD_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|[_\-\s]+")

# Ordered so a token that could plausibly match more than one family (e.g. a
# hypothetical "timestamp_id" is unlikely, but the check order still matters
# for the real families here) resolves the same way regardless of input:
# boolean and temporal spellings never collide with numeric ones, and binary
# is checked before numeric/string so a dialect spelling `raw` or `bytea`
# never lands in string just because it also isn't obviously numeric.
_BOOLEAN_TOKENS = ("bool", "bit")
_DATE_TIME_TOKENS = ("timestamp", "datetime", "date", "time", "interval")
_BINARY_TOKENS = ("binary", "blob", "bytea", "varbinary", "raw")
_NUMERIC_TOKENS = (
    "numeric",
    "number",
    "decimal",
    "bigint",
    "smallint",
    "tinyint",
    "int64",
    "int32",
    "int",
    "float",
    "double",
    "real",
    "serial",
)
_STRING_TOKENS = ("varchar", "nchar", "char", "text", "string", "clob", "json", "uuid", "guid")


def canonical_column_name(name: str) -> str:
    """Normalize a column name so naming-convention differences (snake_case,
    camelCase, PascalCase, SCREAMING_CASE) collapse to the same key.

    ``"CustomerID"``, ``"customerId"``, ``"customer_id"`` and
    ``"CUSTOMER_ID"`` all canonicalize to ``"customer_id"``.
    """
    if not name:
        return ""
    parts = [part for part in _WORD_BOUNDARY.split(name) if part]
    return "_".join(part.lower() for part in parts)


def physical_type_family(physical_type: str) -> str:
    """Bucket a connector-reported physical type string into a coarse family.

    Strips any parenthesized precision/scale (``NUMBER(38,0)``,
    ``VARCHAR(255)``) before matching, so precision differences between two
    otherwise-identical dialect spellings never prevent a family match.
    Returns one of ``NUMERIC``, ``STRING``, ``DATE_TIME``, ``BOOLEAN``,
    ``BINARY``, or ``OTHER`` when nothing recognized matches.
    """
    normalized = physical_type.strip().lower()
    base = normalized.split("(", 1)[0].strip()
    if not base:
        return "OTHER"
    for token in _BOOLEAN_TOKENS:
        if token in base:
            return "BOOLEAN"
    for token in _DATE_TIME_TOKENS:
        if token in base:
            return "DATE_TIME"
    for token in _BINARY_TOKENS:
        if token in base:
            return "BINARY"
    for token in _NUMERIC_TOKENS:
        if token in base:
            return "NUMERIC"
    for token in _STRING_TOKENS:
        if token in base:
            return "STRING"
    return "OTHER"
