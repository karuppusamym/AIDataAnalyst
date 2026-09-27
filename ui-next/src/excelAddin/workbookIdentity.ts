/* ---------------------------------------------------------------------------
   Which Atlas datasource an open workbook belongs to.

   Read from the README sheet `aida.model_export._readme_sheet` writes. The
   server checks the same `Datasource id` row on import
   (`model_import._check_workbook_datasource`), so a workbook cannot be saved
   into a source it was not exported from -- this client reads the row to know
   where to send the file, and the server refuses if anything disagrees. The
   binding is enforced there; here it is only addressing.

   Ids are validated as UUIDs. A cell a steward mistyped must read as "this
   workbook has no Atlas identity", never as a request against whatever the
   mangled text happens to route to.
--------------------------------------------------------------------------- */

export const README_SHEET = "README";

const FIELD_DATASOURCE = "Datasource";
const FIELD_DATASOURCE_ID = "Datasource id";
const FIELD_ORGANIZATION_ID = "Organization id";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export interface WorkbookIdentity {
  readonly datasourceId: string | null;
  readonly organizationId: string | null;
  readonly datasourceName: string | null;
}

export function readWorkbookIdentity(rows: string[][] | null): WorkbookIdentity {
  const fields = new Map<string, string>();
  for (const row of rows ?? []) {
    const field = (row[0] ?? "").trim();
    if (field && !fields.has(field)) fields.set(field, (row[1] ?? "").trim());
  }
  const uuid = (value: string | undefined) =>
    value && UUID_PATTERN.test(value) ? value.toLowerCase() : null;
  return {
    datasourceId: uuid(fields.get(FIELD_DATASOURCE_ID)),
    organizationId: uuid(fields.get(FIELD_ORGANIZATION_ID)),
    datasourceName: fields.get(FIELD_DATASOURCE) || null,
  };
}
