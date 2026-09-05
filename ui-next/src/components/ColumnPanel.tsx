import { useEffect, useState } from "react";
import { ApiError } from "../lib/api";
import {
  fetchColumnDocumentation,
  type ColumnDocumentationRead,
} from "../lib/_column_documentation_api";
import { Pill } from "./primitives";
import "./ColumnPanel.css";

/* ---------------------------------------------------------------------------
   Columns, with their descriptions.

   This surface did not exist. `MetadataColumn.source_description` has been on
   the wire since ingestion started populating it, and the API has always
   returned it -- but the only screen that ever called `/v1/tables/{id}/columns`
   read `{id, name}` out of the response for dropdowns, so a column's
   description had nowhere to appear. Authored column descriptions had nowhere
   to appear either, for the harder reason that nothing stored them until
   `ColumnDocumentation` landed.

   Two rules this encodes, both inherited from `EvidencePane`'s:

   1. Every claim shows where it came from. A source comment and an approved
      business description are not interchangeable -- the first is re-derived
      (and overwritten) by the next rediscovery pass, the second is reviewed
      content that rediscovery never touches. They render as separately
      labelled lines, never merged into "the description".
   2. Absence renders as absence. Most columns in a real catalog have never
      been described, and a pane that quietly showed the source comment under a
      "business description" heading when the authored one was missing would be
      asserting a review that never happened.
--------------------------------------------------------------------------- */

function classificationTone(classification: string): "accent" | "warn" | "mute" {
  if (classification === "RESTRICTED" || classification === "CONFIDENTIAL") return "warn";
  if (classification === "UNCLASSIFIED") return "mute";
  return "accent";
}

function ColumnRow({ column }: { column: ColumnDocumentationRead }) {
  const documented = column.business_description !== null;
  return (
    <li className="colp__row">
      <div className="colp__head">
        <span className="colp__name">{column.name}</span>
        <span className="colp__type">{column.physical_type}</span>
        {!column.nullable ? <span className="colp__flag">not null</span> : null}
        <span className="colp__spacer" />
        <Pill tone={classificationTone(column.classification)}>{column.classification}</Pill>
      </div>

      {documented ? (
        <div className="colp__claim">
          <div className="colp__label">Business description</div>
          <div className="colp__text">{column.business_description}</div>
          <div className="colp__source">
            {`Approved v${column.description_version ?? "?"}`}
            {column.description_approved_by ? ` by ${column.description_approved_by}` : ""}
            {column.description_approved_at
              ? ` · ${new Date(column.description_approved_at).toLocaleDateString()}`
              : ""}
            {column.source_claim_id ? " · from an uploaded data dictionary" : ""}
          </div>
        </div>
      ) : null}

      {column.source_description ? (
        <div className="colp__claim colp__claim--source">
          <div className="colp__label">Source comment</div>
          <div className="colp__text">{column.source_description}</div>
          <div className="colp__source">
            From the source system · replaced on the next rediscovery
          </div>
        </div>
      ) : null}

      {!documented && !column.source_description ? (
        <div className="colp__none">No description.</div>
      ) : null}
    </li>
  );
}

export function ColumnPanel({ tableId }: { tableId: string }) {
  const [columns, setColumns] = useState<ColumnDocumentationRead[] | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    const ac = new AbortController();
    setColumns(null);
    setError(null);
    fetchColumnDocumentation(tableId, ac.signal)
      .then(setColumns)
      .catch((e: unknown) => {
        if ((e as Error)?.name === "AbortError") return;
        setError(e as Error);
      });
    return () => ac.abort();
  }, [tableId]);

  // Collapsed by default: a wide table would otherwise push the evidence items
  // this pane leads with off the screen.
  useEffect(() => {
    setExpanded(false);
  }, [tableId]);

  if (error) {
    return (
      <div className="colp">
        <div className="colp__error" role="alert">
          {error instanceof ApiError && error.status === 403
            ? "You are not authorized to view this table's columns."
            : `Columns could not be loaded: ${
                error instanceof ApiError ? error.detail : error.message
              }`}
        </div>
      </div>
    );
  }

  if (columns === null) {
    return (
      <div className="colp">
        <div className="colp__load" role="status">
          Loading columns…
        </div>
      </div>
    );
  }

  const documentedCount = columns.filter((c) => c.business_description !== null).length;
  const shown = expanded ? columns : columns.slice(0, 8);

  return (
    <div className="colp">
      <div className="colp__sub">
        Columns
        <span className="colp__count">
          {columns.length === 0
            ? "none"
            : `${documentedCount} of ${columns.length} described`}
        </span>
      </div>

      {columns.length === 0 ? (
        <div className="colp__none">This table has no active columns.</div>
      ) : (
        <>
          <ol className="colp__list">
            {shown.map((column) => (
              <ColumnRow key={column.column_id} column={column} />
            ))}
          </ol>
          {columns.length > shown.length ? (
            <button className="colp__more" onClick={() => setExpanded(true)}>
              {`Show ${columns.length - shown.length} more column${
                columns.length - shown.length === 1 ? "" : "s"
              }`}
            </button>
          ) : null}
        </>
      )}
    </div>
  );
}
