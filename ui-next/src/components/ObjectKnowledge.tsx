import { useState } from "react";
import { describeKnowledgeError, downloadOkfBundle, fetchObjectKnowledge } from "../lib/api/knowledge";
import type { ObjectKnowledgeRead } from "../lib/api/knowledge";
import { Button } from "./primitives";
import { useAsyncResource } from "./screenState";
import { KnowledgeDocument } from "./KnowledgeDocument";
import { ObjectSourceKnowledge } from "./ObjectSourceKnowledge";
import "./Knowledge.css";

/* ---------------------------------------------------------------------------
   Knowledge, in Catalog object details (R11-OKF02).

   This object's document as each context product bundle you may read holds it:
   the readable document, its coverage (description state, definition digest and
   capture version), which publication last changed it, and a download of that
   exact publication. One section of the evidence pane -- not a tab, not a route.

   Collapsed until opened, on purpose. Each product bundle behind it is a full
   authorized read (scope, admission, and a rebuild if the source moved), so the
   pane does not pay for it on every row a steward clicks past. A product you
   may not read, or whose bundle does not admit this object's source for you,
   contributes nothing: the empty state says "none you may read", never "N
   hidden".

   When no product bundle holds the object, the server also answers from the
   object's own datasource bundle (`source`), and this shows it: the document,
   an absence, or -- as a refusal, never as an absence -- the source's own "no".
   A product's reading wins: with any product entry, the source is not shown.
--------------------------------------------------------------------------- */

export function ObjectKnowledge({ tableId }: { tableId: string }) {
  const [open, setOpen] = useState(false);
  const knowledge = useAsyncResource<ObjectKnowledgeRead>(
    async (signal) => {
      try {
        return await fetchObjectKnowledge(tableId, signal);
      } catch (reason) {
        if ((reason as Error)?.name === "AbortError") throw reason;
        throw new Error(describeKnowledgeError(reason));
      }
    },
    [tableId],
    { enabled: open },
  );
  const [downloadError, setDownloadError] = useState<string | null>(null);

  return (
    <section className="okn" aria-label="Knowledge">
      <button
        type="button"
        className="okn__toggle"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {open ? "▾" : "▸"} Knowledge
      </button>
      {!open ? null : knowledge.loading ? (
        <p className="kview__note" role="status">
          Reading the published knowledge bundles…
        </p>
      ) : knowledge.error ? (
        <p className="kview__error" role="alert">
          {knowledge.error}
        </p>
      ) : (knowledge.data?.items ?? []).length === 0 ? (
        knowledge.data?.source ? (
          <ObjectSourceKnowledge source={knowledge.data.source} />
        ) : (
          <p className="kview__note">
            No published knowledge bundle you may read includes this object.
          </p>
        )
      ) : (
        <>
          {downloadError ? (
            <p className="kview__error" role="alert">
              {downloadError}
            </p>
          ) : null}
          {(knowledge.data?.items ?? []).map((item) => {
            const coverage = item.coverage as Record<string, unknown>;
            const digest = typeof coverage.definition_digest === "string" ? coverage.definition_digest : null;
            return (
              <div className="okn__item" key={`${item.context_product_version_id}:${item.document.path}`}>
                <div className="okn__product">
                  {item.product_name} · {item.product_key} v{item.product_version}
                </div>
                <div className="okn__facts">
                  <span>publication {item.publication.sequence}</span>
                  <span>
                    {item.document.rendered_in_sequence === item.publication.sequence
                      ? "changed in this publication"
                      : `unchanged since publication ${item.document.rendered_in_sequence}`}
                  </span>
                  <span>description {String(coverage.description_state ?? "NONE").toLowerCase()}</span>
                  {digest ? <code>definition {digest.slice(0, 12)}</code> : null}
                  {coverage.definition_capture_version != null ? (
                    <span>capture v{String(coverage.definition_capture_version)}</span>
                  ) : null}
                </div>
                <KnowledgeDocument content={item.document.content} />
                <div>
                  <Button
                    onClick={() => {
                      setDownloadError(null);
                      void downloadOkfBundle(
                        item.context_product_version_id,
                        item.publication.publication_id,
                      ).catch((reason: unknown) => setDownloadError(describeKnowledgeError(reason)));
                    }}
                    disabled={!item.publication.valid}
                    title="Download the product bundle this document belongs to"
                  >
                    Download bundle
                  </Button>
                </div>
              </div>
            );
          })}
        </>
      )}
    </section>
  );
}
