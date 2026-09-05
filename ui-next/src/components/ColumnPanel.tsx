import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../lib/api";
import {
  fetchColumnDocumentation,
  fetchTableDescription,
  type ColumnDocumentationRead,
  type TableDescriptionRead,
} from "../lib/_column_documentation_api";
import {
  DescriptionActionDialog,
  type DescriptionActionSubject,
} from "./DescriptionActionDialog";
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
   3. A *retired* description is not the same absence. "We looked and decided
      to say nothing" and "nobody has looked" are different facts about an
      asset, so a withdrawn description renders as its own state, showing the
      text that was retired rather than reverting to looking untouched.

   Withdrawing and reinstating are requests, not acts: each files a review that
   someone else decides, and nothing changes until they do. The dialog says so
   rather than the button implying it.

   The table's own documentation renders here too, above its columns. It is the
   same kind of claim about the same asset, and giving it a separate home would
   have meant a steward looking in two places to answer one question -- and,
   more practically, the evidence pane's items are prose, which cannot drive a
   withdraw control that needs to know structurally whether a description
   exists.
--------------------------------------------------------------------------- */

function classificationTone(classification: string): "accent" | "warn" | "mute" {
  if (classification === "RESTRICTED" || classification === "CONFIDENTIAL") return "warn";
  if (classification === "UNCLASSIFIED") return "mute";
  return "accent";
}

function ColumnRow({
  column,
  onAct,
  busy,
}: {
  column: ColumnDocumentationRead;
  onAct: (action: "WITHDRAW" | "REINSTATE", subject: DescriptionActionSubject) => void;
  busy: boolean;
}) {
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
            {" · "}
            <button
              className="colp__withdraw"
              disabled={busy}
              onClick={() =>
                onAct("WITHDRAW", {
                  subjectType: "COLUMN",
                  subjectId: column.column_id,
                  label: column.name,
                  text: column.business_description ?? "",
                })
              }
              title="Ask for this description to be retired. It stays published until a reviewer approves."
            >
              Withdraw
            </button>
          </div>
        </div>
      ) : null}

      {!documented && column.withdrawn_description ? (
        <div className="colp__claim colp__claim--withdrawn">
          <div className="colp__label">Withdrawn description</div>
          <div className="colp__text">{column.withdrawn_description}</div>
          <div className="colp__source">
            Retired after review · this column reads as undescribed{" · "}
            <button
              className="colp__withdraw"
              disabled={busy}
              onClick={() =>
                onAct("REINSTATE", {
                  subjectType: "COLUMN",
                  subjectId: column.column_id,
                  label: column.name,
                  text: column.withdrawn_description ?? "",
                })
              }
              title="Ask for this description to be published again as a new version."
            >
              Reinstate
            </button>
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

      {!documented && !column.source_description && !column.withdrawn_description ? (
        <div className="colp__none">No description.</div>
      ) : null}
    </li>
  );
}

export function ColumnPanel({ tableId }: { tableId: string }) {
  const [columns, setColumns] = useState<ColumnDocumentationRead[] | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [table, setTable] = useState<TableDescriptionRead | null>(null);
  const [pending, setPending] = useState<
    { action: "WITHDRAW" | "REINSTATE"; subject: DescriptionActionSubject } | null
  >(null);

  const act = useCallback(
    (action: "WITHDRAW" | "REINSTATE", subject: DescriptionActionSubject) => {
      setNotice(null);
      setPending({ action, subject });
    },
    [],
  );

  const load = useCallback(
    (signal?: AbortSignal) => {
      setError(null);
      fetchColumnDocumentation(tableId, signal)
        .then(setColumns)
        .catch((e: unknown) => {
          if ((e as Error)?.name === "AbortError") return;
          setError(e as Error);
        });
      // The table's own documentation failing must not blank the column list:
      // they are separate claims about the same asset.
      fetchTableDescription(tableId, signal)
        .then(setTable)
        .catch(() => setTable(null));
    },
    [tableId],
  );

  useEffect(() => {
    const ac = new AbortController();
    setColumns(null);
    setTable(null);
    load(ac.signal);
    return () => ac.abort();
  }, [load]);

  // Collapsed by default: a wide table would otherwise push the evidence items
  // this pane leads with off the screen.
  useEffect(() => {
    setExpanded(false);
    setNotice(null);
    setPending(null);
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
      {/* The table's own documentation, above its columns: the same kind of
          claim about the same asset, so looking in two places to answer one
          question would be the wrong shape. */}
      {table && (table.readme || table.withdrawn_readme) ? (
        <div className="colp__table">
          <div className="colp__sub">Table description</div>
          {table.readme ? (
            <div className="colp__claim">
              <div className="colp__text">{table.readme}</div>
              <div className="colp__source">
                {`Approved v${table.readme_version ?? "?"}`}
                {table.approved_by ? ` by ${table.approved_by}` : ""}
                {" · "}
                <button
                  className="colp__withdraw"
                  disabled={pending !== null}
                  onClick={() =>
                    act("WITHDRAW", {
                      subjectType: "TABLE",
                      subjectId: table.table_id,
                      label: table.name,
                      text: table.readme ?? "",
                    })
                  }
                  title="Ask for this table's documentation to be retired."
                >
                  Withdraw
                </button>
              </div>
            </div>
          ) : (
            <div className="colp__claim colp__claim--withdrawn">
              <div className="colp__text">{table.withdrawn_readme}</div>
              <div className="colp__source">
                Retired after review · this table reads as undocumented{" · "}
                <button
                  className="colp__withdraw"
                  disabled={pending !== null}
                  onClick={() =>
                    act("REINSTATE", {
                      subjectType: "TABLE",
                      subjectId: table.table_id,
                      label: table.name,
                      text: table.withdrawn_readme ?? "",
                    })
                  }
                  title="Ask for this documentation to be published again as a new version."
                >
                  Reinstate
                </button>
              </div>
            </div>
          )}
        </div>
      ) : null}

      {pending ? (
        <DescriptionActionDialog
          action={pending.action}
          subject={pending.subject}
          onClose={() => setPending(null)}
          onRequested={(message) => {
            setNotice(message);
            // Re-read rather than patching state: the request may have been
            // refused for a reason the server knows and this component does not.
            load();
          }}
        />
      ) : null}

      <div className="colp__sub">
        Columns
        <span className="colp__count">
          {columns.length === 0
            ? "none"
            : `${documentedCount} of ${columns.length} described`}
        </span>
      </div>

      {notice ? (
        <div className="colp__notice" role="status">
          {notice}
        </div>
      ) : null}
      {columns.length === 0 ? (
        <div className="colp__none">This table has no active columns.</div>
      ) : (
        <>
          <ol className="colp__list">
            {shown.map((column) => (
              <ColumnRow
                key={column.column_id}
                column={column}
                onAct={act}
                busy={pending !== null}
              />
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
