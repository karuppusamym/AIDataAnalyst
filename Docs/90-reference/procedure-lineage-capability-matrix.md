# Procedure lineage parser capability matrix

Generated 2026-09-02T15:48:45.619696+00:00 by `scripts/generate_procedure_capability_matrix.py` (`aida.procedure_capability_matrix.build_capability_matrix`) -- every status below is read directly out of `sql_lineage_parser.py`'s and `procedure_lineage.py`'s own dispatch code at generation time, not hand-maintained prose. Regenerate after any change to either module's dispatcher; do not hand-edit this file.

## Dialects attempted

`bigquery, oracle, postgres, snowflake, tsql` -- read from `sql_lineage_parser._SQLGLOT_DIALECT_MAP`, shared by both parsers. A dialect not in this list is refused outright by both (`unsupported dialect: ...`, `Confidence.LOW`), never silently guessed at.

## Constructs

| Construct | View/flat-DML parser (`sql_lineage_parser.py`) | Procedure-aware parser (`procedure_lineage.py`) |
|---|---|---|
| SELECT (standalone read) | SUPPORTED | SUPPORTED |
| UNION | SUPPORTED | SUPPORTED |
| CREATE VIEW / CREATE TABLE AS SELECT | SUPPORTED | SUPPORTED |
| INSERT ... SELECT | SUPPORTED | SUPPORTED |
| UPDATE ... SET / UPDATE ... FROM | N/A | SUPPORTED |
| DELETE | N/A | SUPPORTED |
| MERGE | SUPPORTED | SUPPORTED |
| EXEC/EXECUTE (dynamic or nested call) | N/A | EXPLICIT_UNPARSED |
| sp_executesql | N/A | EXPLICIT_UNPARSED |
| unrecognised statement shape (sqlglot Command fallback) | N/A | EXPLICIT_UNPARSED |
| IF ... BEGIN (T-SQL) | N/A | SUPPORTED |
| IF/ELSIF ... THEN (PL/SQL) | N/A | SUPPORTED |
| WHILE ... BEGIN (T-SQL) | N/A | SUPPORTED |
| WHILE ... LOOP (PL/SQL) | N/A | SUPPORTED |
| CASE ... WHEN ... THEN (PL/SQL statement form) | N/A | SUPPORTED |
| cursor FOR ... IN (SELECT ...) LOOP (PL/SQL) | N/A | SUPPORTED |
| bare FOR ... LOOP (PL/SQL) | N/A | SUPPORTED |
| EXECUTE IMMEDIATE / EXEC(...) / sp_executesql (dynamic SQL) | N/A | EXPLICIT_UNPARSED |
| EXEC/CALL <procedure_name> (nested procedure call) | N/A | EXPLICIT_UNPARSED |
| DECLARE/SET/OPEN/FETCH/CLOSE/RAISERROR/... (no table lineage) | N/A | RECOGNISED_NO_LINEAGE |

`SUPPORTED` -- real column/table-level lineage extracted.
`EXPLICIT_UNPARSED` -- recognised, but this parser cannot safely resolve it: an explicit `UNPARSED` marker edge is produced instead (INV-9/AT-C4), never a silent drop.
`RECOGNISED_NO_LINEAGE` -- recognised, and genuinely carries no table lineage (e.g. `DECLARE`/`SET`) -- correctly skipped, not a gap.
`UNSUPPORTED` -- no dispatch branch recognises this construct at all.
`N/A` -- not a shape that construct's own contract covers (e.g. control flow inside a bare `CREATE VIEW` definition).

## Explicit degradation reasons (procedure-aware parser)

Every `EXPLICIT_UNPARSED` row above surfaces as one of these named reasons on the UNPARSED marker edge (`aida.procedure_lineage.UnparsedReason`):

- `DYNAMIC_SQL`
- `NESTED_PROCEDURE_CALL`
- `UNSUPPORTED_STATEMENT_SHAPE`
- `PARSE_ERROR`
- `UNRESOLVED_CONTROL_FLOW`
