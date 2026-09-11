/* ---------------------------------------------------------------------------
   Demo data for the Data dictionaries screen (fixture mode only).

   Its own module rather than a block in `fixtures.ts`: it keeps a small store
   so upload -> match -> propose behaves the way the server does, and nothing
   else reads it. The rules follow `src/aida/document_ingestion.py`: a row needs
   a table and a description, matching is exact and never guesses, and a
   document's rows can be proposed for review once.
--------------------------------------------------------------------------- */

import type { DocumentCreate, DocumentMappingSummaryRead, DocumentRead } from "./types";
import type {
  DocumentClaimRead,
  DocumentMappingRead,
  DocumentSectionRead,
  PageOf,
} from "./ui-types";

interface StoredDocument {
  document: DocumentRead;
  sections: DocumentSectionRead[];
  mappings: DocumentMappingRead[];
  claims: DocumentClaimRead[];
}

interface ParsedRow {
  schema: string | null;
  table: string;
  column: string | null;
  description: string;
}

/** The demo catalog, by table name: the columns each table has. */
const DEMO_CATALOG: Readonly<Record<string, readonly string[]>> = {
  customers: ["customer_id", "full_name", "segment_code"],
  accounts: ["account_id", "customer_id", "opened_on"],
  payments: ["payment_id", "amount", "amount_ccy", "settled_at"],
};

const DEMO_ORG = "00000000-0000-0000-0000-000000000001";
const MAX_BYTES = 1_000_000;

const SEED_CSV = [
  "schema,table,column,description",
  'public,customers,segment_code,"Customer segment set at onboarding: RETAIL, PRIVATE or SME."',
  "public,payments,amount_ccy,ISO 4217 currency of the payment amount.",
  "public,payments,,One row per settled payment instruction.",
  "legacy,gl_postings,gl_code,General-ledger account the posting was booked to.",
].join("\n");

const store = new Map<string, StoredDocument>();
const seeded = new Set<string>();
let sequence = 0;

function nextId(prefix: string): string {
  sequence += 1;
  return `${prefix}-demo-${sequence}`;
}

function page<T>(items: T[]): PageOf<T> {
  return { items, limit: 500, offset: 0, total: items.length };
}

function pause(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 60));
}

/** One CSV record's fields: quoted fields, doubled quotes, commas in quotes. */
function splitRecord(line: string): string[] {
  const fields: string[] = [];
  let current = "";
  let quoted = false;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line.charAt(i);
    if (quoted) {
      if (ch === '"' && line.charAt(i + 1) === '"') {
        current += '"';
        i += 1;
      } else if (ch === '"') {
        quoted = false;
      } else {
        current += ch;
      }
    } else if (ch === '"') {
      quoted = true;
    } else if (ch === ",") {
      fields.push(current);
      current = "";
    } else {
      current += ch;
    }
  }
  fields.push(current);
  return fields;
}

/** Header-aware and case-insensitive, like `parse_csv_data_dictionary`. */
function parseRows(content: string): { rows: ParsedRow[]; errors: number } {
  const lines = content.split(/\r?\n/).filter((line) => line.trim() !== "");
  const [header, ...records] = lines;
  if (header === undefined) return { rows: [], errors: 0 };
  const names = splitRecord(header).map((name) => name.trim().toLowerCase());
  const field = (fields: string[], key: string): string => {
    const index = names.indexOf(key);
    return index < 0 ? "" : (fields[index] ?? "").trim();
  };
  const rows: ParsedRow[] = [];
  let errors = 0;
  for (const record of records) {
    const fields = splitRecord(record);
    const table = field(fields, "table");
    const description = field(fields, "description");
    if (!table || !description) {
      errors += 1;
      continue;
    }
    rows.push({
      schema: field(fields, "schema") || null,
      table,
      column: field(fields, "column") || null,
      description,
    });
  }
  return { rows, errors };
}

function createDocument(
  projectId: string,
  filename: string,
  content: string,
  uploadedBy: string,
): StoredDocument {
  const { rows, errors } = parseRows(content);
  const now = new Date().toISOString();
  const id = nextId("document");
  const stored: StoredDocument = {
    document: {
      id,
      organization_id: DEMO_ORG,
      project_id: projectId,
      filename,
      media_type: "CSV",
      sha256: "demo",
      status: "PARSED",
      section_count: rows.length,
      parse_error_count: errors,
      uploaded_by: uploadedBy,
      created_at: now,
      updated_at: now,
    },
    sections: rows.map(
      (row, ordinal): DocumentSectionRead => ({
        id: nextId("section"),
        document_id: id,
        ordinal,
        raw_schema_name: row.schema,
        raw_table_name: row.table,
        raw_column_name: row.column,
        raw_description: row.description,
      }),
    ),
    mappings: [],
    claims: [],
  };
  store.set(id, stored);
  return stored;
}

function match(stored: StoredDocument): DocumentMappingSummaryRead {
  stored.mappings = stored.sections.map((section): DocumentMappingRead => {
    const columns = DEMO_CATALOG[section.raw_table_name.toLowerCase()];
    const column = section.raw_column_name?.toLowerCase() ?? null;
    const found = columns !== undefined && (column === null || columns.includes(column));
    return {
      id: nextId("mapping"),
      document_section_id: section.id,
      subject_type: column === null ? "TABLE" : "COLUMN",
      subject_id: found ? `${section.raw_table_name}${column === null ? "" : `.${column}`}` : null,
      mapping_kind: found ? "STRUCTURAL" : "UNMATCHED",
      confidence: found ? 1 : 0,
    };
  });
  stored.document = { ...stored.document, status: "MAPPED", updated_at: new Date().toISOString() };
  const matched = stored.mappings.filter((mapping) => mapping.mapping_kind === "STRUCTURAL").length;
  return {
    document_id: stored.document.id,
    matched_count: matched,
    unmatched_count: stored.mappings.length - matched,
  };
}

function propose(stored: StoredDocument, requestedBy: string): DocumentClaimRead[] {
  const sectionById = new Map(stored.sections.map((section) => [section.id, section]));
  stored.claims = stored.mappings
    .filter((mapping) => mapping.mapping_kind === "STRUCTURAL" && mapping.subject_id !== null)
    .map(
      (mapping): DocumentClaimRead => ({
        id: nextId("claim"),
        document_section_id: mapping.document_section_id,
        subject_type: mapping.subject_type,
        subject_id: mapping.subject_id ?? "",
        predicate: "DESCRIBES",
        object_value: sectionById.get(mapping.document_section_id)?.raw_description ?? "",
        confidence: mapping.confidence,
        status: "PENDING",
        governance_review_id: nextId("review"),
        created_by: requestedBy,
        reviewed_by: null,
        reviewed_at: null,
      }),
    );
  return stored.claims;
}

/** One example per project: matched, proposed, and one row already approved. */
function seed(projectId: string): void {
  if (seeded.has(projectId)) return;
  seeded.add(projectId);
  const stored = createDocument(projectId, "core_banking_dictionary.csv", SEED_CSV, "demo-steward");
  match(stored);
  const [first] = propose(stored, "demo-steward");
  if (first) {
    first.status = "APPROVED";
    first.reviewed_by = "demo-reviewer";
    first.reviewed_at = stored.document.created_at;
  }
}

function find(documentId: string): StoredDocument {
  const stored = store.get(documentId);
  if (!stored) throw new Error("document not found");
  return stored;
}

export async function fixtureProjectDocuments(projectId: string): Promise<PageOf<DocumentRead>> {
  await pause();
  seed(projectId);
  const documents = [...store.values()]
    .map((stored) => stored.document)
    .filter((document) => document.project_id === projectId)
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
  return page(documents);
}

export async function fixtureUploadDataDictionary(
  projectId: string,
  body: DocumentCreate,
): Promise<DocumentRead> {
  await pause();
  if (new TextEncoder().encode(body.content).length > MAX_BYTES) {
    throw new Error(`document content exceeds ${MAX_BYTES} bytes`);
  }
  return createDocument(projectId, body.filename, body.content, "demo-steward").document;
}

export async function fixtureDocumentSections(
  documentId: string,
): Promise<PageOf<DocumentSectionRead>> {
  await pause();
  return page(find(documentId).sections);
}

export async function fixtureMapDocument(documentId: string): Promise<DocumentMappingSummaryRead> {
  await pause();
  const stored = find(documentId);
  if (stored.document.status !== "PARSED") {
    throw new Error("document must be in PARSED status to be mapped");
  }
  return match(stored);
}

export async function fixtureDocumentMappings(
  documentId: string,
): Promise<PageOf<DocumentMappingRead>> {
  await pause();
  return page(find(documentId).mappings);
}

export async function fixtureExtractDocumentClaims(
  documentId: string,
): Promise<PageOf<DocumentClaimRead>> {
  await pause();
  const stored = find(documentId);
  if (stored.document.status !== "MAPPED") {
    throw new Error("document must be in MAPPED status before extracting claims");
  }
  if (stored.claims.length > 0) {
    throw new Error(
      "descriptions were already proposed for this document; decide them in the review queue",
    );
  }
  return page(propose(stored, "demo-steward"));
}

export async function fixtureDocumentClaims(
  documentId: string,
): Promise<PageOf<DocumentClaimRead>> {
  await pause();
  return page(find(documentId).claims);
}
