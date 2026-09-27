import { useState } from "react";
import {
  SOURCE_BUNDLE_READABLE_ONLY,
  describeKnowledgeError,
  downloadSourceOkfBundle,
} from "../lib/api/knowledge";
import type { ObjectKnowledgeSource } from "../lib/api/knowledge";
import { Button } from "./primitives";
import { KnowledgeDocument } from "./KnowledgeDocument";
import "./Knowledge.css";

/* ---------------------------------------------------------------------------
   The datasource's own bundle, in Catalog object details (R11-OKF02).

   Shown only when no context product bundle holds the object -- a product's
   reading is the one a steward asked for and wins, so `ObjectKnowledge` renders
   this only after it has found no product entry. The server resolves it: a
   document's path is a digest of catalog, schema and name, which the catalog
   never exposes, so this component neither forms a path nor makes a second
   request.

   THREE STATES, TOLD APART BY WHAT THEY SAY, never by one being a quieter
   version of another:

     DOCUMENT       the object's document from this source's stored bundle, read
                    exactly as a product's is, with the publication it came from.
     NOT_IN_BUNDLE  the bundle you may read holds no document for it. Said as an
                    absence, and only as that: an object never discovered, a
                    retired one and one in a schema your workspace refuses read
                    alike, so this does not say which.
     REFUSED        the source's own read decision refused you. Said as a
                    refusal, in an alert, with the server's bare reason code --
                    never the quiet "no document" an absence gets, because being
                    refused and there being nothing are different facts to act on.

   The sentence about what a source bundle holds is the one the Sources screen
   says beside "Open knowledge bundle", word for word.
--------------------------------------------------------------------------- */

export function ObjectSourceKnowledge({ source }: { source: ObjectKnowledgeSource }) {
  const [downloadError, setDownloadError] = useState<string | null>(null);

  if (source.state === "REFUSED") {
    return (
      <p className="kview__error" role="alert">
        You are not permitted to read this object&rsquo;s data source bundle.{" "}
        <code>{source.reason}</code>
      </p>
    );
  }

  if (source.state === "NOT_IN_BUNDLE") {
    return (
      <>
        <p className="kview__note">
          No context product you may read includes this object, and the bundle of{" "}
          {source.datasource_name} holds no document for it that you may read.
        </p>
        <p className="kview__note">{SOURCE_BUNDLE_READABLE_ONLY}</p>
      </>
    );
  }

  if (source.state !== "DOCUMENT") {
    // A state this view does not know is not an absence. Say so rather than show nothing.
    return (
      <p className="kview__error" role="alert">
        This object&rsquo;s data source bundle answered in a way this view cannot show.
      </p>
    );
  }

  const coverage = source.coverage;
  const digest = typeof coverage.definition_digest === "string" ? coverage.definition_digest : null;
  const { publication, document } = source;
  return (
    <>
      <p className="kview__note">
        No context product you may read includes this object. This is its document from the bundle
        of its own data source.
      </p>
      <div className="okn__item">
        <div className="okn__product">{source.datasource_name} · data source bundle</div>
        <div className="okn__facts">
          <span>publication {publication.sequence}</span>
          <span>
            {document.rendered_in_sequence === publication.sequence
              ? "changed in this publication"
              : `unchanged since publication ${document.rendered_in_sequence}`}
          </span>
          <span>description {String(coverage.description_state ?? "NONE").toLowerCase()}</span>
          {digest ? <code>definition {digest.slice(0, 12)}</code> : null}
          {coverage.definition_capture_version != null ? (
            <span>capture v{String(coverage.definition_capture_version)}</span>
          ) : null}
        </div>
        <KnowledgeDocument content={document.content} />
        <p className="kview__note">{SOURCE_BUNDLE_READABLE_ONLY}</p>
        {downloadError ? (
          <p className="kview__error" role="alert">
            {downloadError}
          </p>
        ) : null}
        <div>
          <Button
            onClick={() => {
              setDownloadError(null);
              void downloadSourceOkfBundle(source.datasource_id, publication.publication_id).catch(
                (reason: unknown) => setDownloadError(describeKnowledgeError(reason)),
              );
            }}
            disabled={!publication.valid}
            title="Download the data source bundle this document belongs to"
          >
            Download bundle
          </Button>
        </div>
      </div>
    </>
  );
}
