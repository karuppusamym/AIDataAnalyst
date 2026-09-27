import { useCallback, useMemo, useRef } from "react";
import type { DocumentMappingSummaryRead, DocumentRead } from "../lib/types";
import type {
  DocumentClaimRead,
  DocumentMappingRead,
  DocumentSectionRead,
  PageOf,
} from "../lib/ui-types";
import {
  DATA_DICTIONARY_MAX_BYTES,
  DATA_DICTIONARY_MAX_ROWS,
  extractDocumentClaims,
  fetchDocumentClaims,
  fetchDocumentMappings,
  fetchDocumentSections,
  fetchProjectDocuments,
  mapDocument,
  uploadDataDictionary,
} from "../lib/api";
import { buildRelativeLink } from "../lib/routes";
import { useScopeSelection } from "../lib/scope";
import { useUrlState } from "../lib/useUrlState";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import {
  FormError,
  FormSuccess,
  LoadingPanel,
  useAsyncResource,
  useSubmitAction,
} from "../components/screenState";
import type { AsyncResource } from "../components/screenState";
import "./DataDictionariesScreen.css";

/* ---------------------------------------------------------------------------
   Data dictionaries: the fourth way a column description gets written, and
   until this screen the only one nobody could reach (doc 17's write-path
   table; `document_ingestion_api.py` had seven routes and no caller).

   A steward uploads a data dictionary another tool exported, as CSV. The server
   keeps each row that names a table and a description; `schema` and `column`
   are optional. Matching resolves every row by exact name against the ACTIVE
   tables and columns of this project's sources. A row that matches nothing,
   or more than one table, stays unmatched instead of being guessed. Proposing
   turns each matched row into its own review, and only an approval in the
   review queue publishes the text.

   Nothing on this screen publishes anything, so each step says what it will do
   before it is pressed.
--------------------------------------------------------------------------- */

const DOCUMENT_STATUS: Record<string, { label: string; tone: Tone }> = {
  PARSED: { label: "Uploaded", tone: "info" },
  MAPPED: { label: "Matched", tone: "ok" },
};

const CLAIM_STATUS: Record<string, { label: string; tone: Tone }> = {
  PENDING: { label: "In review", tone: "info" },
  APPROVED: { label: "Published", tone: "ok" },
  REJECTED: { label: "Rejected", tone: "bad" },
};

const UNKNOWN_TONE: Tone = "mute";

function statusOf(table: Record<string, { label: string; tone: Tone }>, status: string) {
  return table[status] ?? { label: status, tone: UNKNOWN_TONE };
}

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count.toLocaleString()} ${count === 1 ? one : many}`;
}

function subjectOf(section: DocumentSectionRead): string {
  return [section.raw_schema_name, section.raw_table_name, section.raw_column_name]
    .filter((part): part is string => Boolean(part))
    .join(".");
}

interface DocumentDetail {
  sections: DocumentSectionRead[];
  sectionTotal: number;
  mappings: Map<string, DocumentMappingRead>;
  claims: Map<string, DocumentClaimRead>;
  claimTotal: number;
}

function Header({ projectName }: { projectName: string | null }) {
  return (
    <header className="ddict__head">
      <h1 className="ddict__h1">Data dictionaries</h1>
      <p className="ddict__lede">
        Bring in the table and column descriptions another tool already holds. Upload its data
        dictionary as CSV, match the rows to the catalog
        {projectName ? (
          <>
            {" "}
            of <strong>{projectName}</strong>
          </>
        ) : null}
        , and propose them for review. Each matched row becomes its own review, and nothing is
        published until a reviewer approves it.
      </p>
    </header>
  );
}

function UploadForm({
  projectId,
  ready,
  onUploaded,
}: {
  projectId: string;
  ready: boolean;
  onUploaded: (document: DocumentRead) => void;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const action = useSubmitAction<DocumentRead>();

  const submit = useCallback(async () => {
    const file = fileRef.current?.files?.[0];
    if (!file) {
      action.fail("Choose a CSV file first.");
      return;
    }
    if (file.size > DATA_DICTIONARY_MAX_BYTES) {
      action.fail(
        "This file is over 1 MB, the most the server accepts in one upload. Split it and upload the parts.",
      );
      return;
    }
    const uploaded = await action.run(async () =>
      uploadDataDictionary(projectId, { filename: file.name, content: await file.text() }),
    );
    if (!uploaded) return;
    if (fileRef.current) fileRef.current.value = "";
    onUploaded(uploaded);
  }, [projectId, action, onUploaded]);

  return (
    <form
      className="ddict__upload"
      aria-label="Upload a data dictionary"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <Field label="Data dictionary (CSV)">
        <input ref={fileRef} type="file" accept=".csv,text/csv" />
      </Field>
      <p className="ddict__hint">
        Columns named <code>table</code> and <code>description</code> are required; <code>schema</code>{" "}
        and <code>column</code> are optional, and header case does not matter. A row with no table or
        no description is skipped and counted. Up to 1 MB and{" "}
        {DATA_DICTIONARY_MAX_ROWS.toLocaleString()} rows.
      </p>
      {action.error ? <FormError detail={action.error} /> : null}
      {action.result ? (
        <FormSuccess>
          {`Uploaded ${action.result.filename}: ${plural(action.result.section_count, "row")} kept`}
          {action.result.parse_error_count
            ? `, ${plural(action.result.parse_error_count, "row")} skipped`
            : ""}
          .
        </FormSuccess>
      ) : null}
      <Button type="submit" variant="primary" disabled={!ready || action.submitting}>
        {action.submitting ? "Uploading…" : "Upload"}
      </Button>
    </form>
  );
}

function ClaimCell({ claim }: { claim: DocumentClaimRead }) {
  const status = statusOf(CLAIM_STATUS, claim.status);
  return (
    <span className="ddict__review">
      <Pill tone={status.tone}>{status.label}</Pill>
      {claim.governance_review_id ? (
        <a
          href={buildRelativeLink({
            screen: "governance",
            params: { review: claim.governance_review_id },
          })}
        >
          Open in review queue
        </a>
      ) : null}
    </span>
  );
}

function RowsTable({ document, detail }: { document: DocumentRead; detail: DocumentDetail }) {
  if (detail.sections.length === 0) {
    return <Empty title="No rows were kept" hint="Every row was missing a table or a description." />;
  }
  return (
    <>
      <div className="ddict__tablewrap">
        <table className="ddict__rows">
          <caption className="ddict__caption">Rows in {document.filename}</caption>
          <thead>
            <tr>
              <th scope="col">Row</th>
              <th scope="col">Describes</th>
              <th scope="col">Description</th>
              <th scope="col">Catalog</th>
              <th scope="col">Review</th>
            </tr>
          </thead>
          <tbody>
            {detail.sections.map((section) => {
              const mapping = detail.mappings.get(section.id);
              const claim = detail.claims.get(section.id);
              return (
                <tr key={section.id}>
                  <td>{section.ordinal + 1}</td>
                  <td>
                    <code>{subjectOf(section)}</code>
                    <span className="ddict__kind">{section.raw_column_name ? "column" : "table"}</span>
                  </td>
                  <td className="ddict__text">{section.raw_description}</td>
                  <td>
                    {mapping === undefined ? (
                      <span className="ddict__muted">Not matched yet</span>
                    ) : mapping.mapping_kind === "STRUCTURAL" ? (
                      <Pill tone="ok">Matched</Pill>
                    ) : (
                      <Pill tone="warn">No match</Pill>
                    )}
                  </td>
                  <td>{claim ? <ClaimCell claim={claim} /> : <span className="ddict__muted">—</span>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {detail.sectionTotal > detail.sections.length ? (
        <p className="ddict__hint">
          Showing the first {detail.sections.length.toLocaleString()} of{" "}
          {detail.sectionTotal.toLocaleString()} rows.
        </p>
      ) : null}
    </>
  );
}

function DocumentPanel({
  document,
  detail,
  onChanged,
}: {
  document: DocumentRead;
  detail: AsyncResource<DocumentDetail>;
  onChanged: () => void;
}) {
  const matchAction = useSubmitAction<DocumentMappingSummaryRead>();
  const proposeAction = useSubmitAction<PageOf<DocumentClaimRead>>();
  const data = detail.data;
  const mappings = data ? [...data.mappings.values()] : [];
  const matched = mappings.filter((mapping) => mapping.mapping_kind === "STRUCTURAL").length;
  const unmatched = mappings.length - matched;
  const claims = data ? [...data.claims.values()] : [];
  const claimsIn = (status: DocumentClaimRead["status"]) =>
    claims.filter((claim) => claim.status === status).length;
  const status = statusOf(DOCUMENT_STATUS, document.status);

  const runMatch = async () => {
    if (await matchAction.run(() => mapDocument(document.id))) onChanged();
  };
  const runPropose = async () => {
    if (await proposeAction.run(() => extractDocumentClaims(document.id))) onChanged();
  };

  return (
    <div className="ddict__panel">
      <div className="ddict__title">
        <h2>{document.filename}</h2>
        <Pill tone={status.tone}>{status.label}</Pill>
      </div>
      <p className="ddict__summary">
        {plural(document.section_count, "row")} kept
        {document.parse_error_count
          ? ` · ${plural(document.parse_error_count, "row")} skipped for having no table or no description`
          : ""}
        {mappings.length ? ` · ${matched} matched · ${unmatched} not matched` : ""}
        {claims.length
          ? ` · ${claimsIn("PENDING")} in review · ${claimsIn("APPROVED")} published · ${claimsIn("REJECTED")} rejected`
          : ""}
      </p>
      {unmatched > 0 ? (
        <p className="ddict__hint">
          A row stays unmatched when its table is not in this project&rsquo;s sources, when the names
          differ, or when two sources both have a table by that name. Adding the schema to the row
          tells them apart.
        </p>
      ) : null}

      {document.status === "PARSED" ? (
        <div className="ddict__step">
          <p>
            Match each row to a table or column in this project&rsquo;s sources, by exact name. A row
            that matches nothing, or more than one table, stays unmatched. Nothing is guessed and
            nothing is published.
          </p>
          <Button variant="primary" onClick={() => void runMatch()} disabled={matchAction.submitting}>
            {matchAction.submitting ? "Matching…" : "Match rows to the catalog"}
          </Button>
          {matchAction.error ? <FormError detail={matchAction.error} /> : null}
        </div>
      ) : null}

      {document.status === "MAPPED" && data && data.claimTotal === 0 ? (
        <div className="ddict__step">
          <p>
            {matched === 0
              ? "No row matched the catalog, so there is nothing to propose."
              : `Each of the ${plural(matched, "matched row")} becomes a proposed description with its own review. A reviewer approves or rejects each one in the review queue, and only an approval publishes it.`}
          </p>
          {matched > 0 ? (
            <Button
              variant="primary"
              onClick={() => void runPropose()}
              disabled={proposeAction.submitting}
            >
              {proposeAction.submitting ? "Proposing…" : `Propose ${plural(matched, "row")} for review`}
            </Button>
          ) : null}
          {proposeAction.error ? <FormError detail={proposeAction.error} /> : null}
        </div>
      ) : null}

      {matchAction.result ? (
        <FormSuccess>
          {`Matched ${plural(matchAction.result.matched_count, "row")}; ${matchAction.result.unmatched_count} not matched.`}
        </FormSuccess>
      ) : null}
      {proposeAction.result ? (
        <FormSuccess>
          {`${plural(proposeAction.result.total, "review")} opened. Decide them in the review queue.`}
        </FormSuccess>
      ) : null}

      {detail.loading && !data ? <LoadingPanel label="Loading rows…" /> : null}
      {detail.error ? (
        <ErrorState title="The rows could not be loaded" detail={detail.error} onRetry={detail.reload} />
      ) : null}
      {data ? <RowsTable document={document} detail={data} /> : null}
    </div>
  );
}

export function DataDictionariesScreen() {
  const scope = useScopeSelection();
  const projectId = scope?.projectId ?? "";
  const projectName = scope?.projects.find((item) => item.id === projectId)?.name ?? null;
  const [params, setParams] = useUrlState();
  const selectedId = params.get("document");

  const documents = useAsyncResource<DocumentRead[]>(
    async (signal) => (await fetchProjectDocuments(projectId, signal)).items,
    [projectId],
    { enabled: projectId !== "" },
  );
  const selected = useMemo(
    () => documents.data?.find((document) => document.id === selectedId) ?? null,
    [documents.data, selectedId],
  );

  const detail = useAsyncResource<DocumentDetail>(
    async (signal) => {
      const id = selectedId ?? "";
      const [sections, mappings, claims] = await Promise.all([
        fetchDocumentSections(id, signal),
        fetchDocumentMappings(id, signal),
        fetchDocumentClaims(id, signal),
      ]);
      return {
        sections: sections.items,
        sectionTotal: sections.total,
        mappings: new Map(mappings.items.map((mapping) => [mapping.document_section_id, mapping])),
        claims: new Map(claims.items.map((claim) => [claim.document_section_id, claim])),
        claimTotal: claims.total,
      };
    },
    [selectedId],
    { enabled: Boolean(selectedId) },
  );

  const select = useCallback((id: string) => setParams({ document: id }), [setParams]);
  const reloadAll = useCallback(() => {
    documents.reload();
    detail.reload();
  }, [documents, detail]);
  const onUploaded = useCallback(
    (document: DocumentRead) => {
      documents.setData((previous) => [
        document,
        ...(previous ?? []).filter((item) => item.id !== document.id),
      ]);
      select(document.id);
    },
    [documents, select],
  );

  if (!projectId) {
    return (
      <section className="ddict">
        <Header projectName={null} />
        <Empty
          title="Choose a project"
          hint="A data dictionary belongs to a project, and its rows are matched against that project's sources. Pick one in the scope picker."
        />
      </section>
    );
  }

  return (
    <section className="ddict">
      <Header projectName={projectName} />
      <div className="ddict__grid">
        <div className="ddict__side">
          <div className="ddict__card">
            <h2>Upload</h2>
            <UploadForm projectId={projectId} ready={scope?.ready ?? false} onUploaded={onUploaded} />
          </div>
          <div className="ddict__card">
            <h2>Uploaded</h2>
            {documents.loading && !documents.data ? (
              <LoadingPanel label="Loading data dictionaries…" />
            ) : null}
            {documents.error ? (
              <ErrorState
                title="Data dictionaries could not be loaded"
                detail={documents.error}
                onRetry={documents.reload}
              />
            ) : null}
            {documents.data && documents.data.length === 0 ? (
              <Empty title="None yet" hint="Upload a CSV to start." />
            ) : null}
            {documents.data && documents.data.length > 0 ? (
              <ul className="ddict__docs" aria-label="Uploaded data dictionaries">
                {documents.data.map((document) => {
                  const status = statusOf(DOCUMENT_STATUS, document.status);
                  return (
                    <li key={document.id}>
                      <button
                        type="button"
                        className="ddict__docbtn"
                        aria-current={document.id === selectedId ? "true" : undefined}
                        onClick={() => select(document.id)}
                      >
                        <span className="ddict__docname">{document.filename}</span>
                        <Pill tone={status.tone}>{status.label}</Pill>
                        <span className="ddict__docmeta">
                          {plural(document.section_count, "row")} · {document.uploaded_by} ·{" "}
                          {new Date(document.created_at).toLocaleString()}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            ) : null}
          </div>
        </div>
        <div className="ddict__card ddict__main">
          {selected ? (
            <DocumentPanel document={selected} detail={detail} onChanged={reloadAll} />
          ) : (
            <Empty
              title="Choose a data dictionary"
              hint="Pick one on the left to see its rows, match them to the catalog and propose them for review."
            />
          )}
        </div>
      </div>
    </section>
  );
}
