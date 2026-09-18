import { useCallback, useMemo, useState } from "react";
import type { OkfBundleRead, OkfDocumentRead, OkfPublicationHistoryRead, OkfPublicationRead } from "../lib/types";
import {
  describeKnowledgeError,
  downloadOkfBundle,
  fetchOkfBundle,
  fetchOkfDocument,
  fetchOkfPublications,
} from "../lib/api/knowledge";
import { Button, Empty, Pill } from "./primitives";
import { LoadingPanel, useAsyncResource } from "./screenState";
import { KnowledgeDocument } from "./KnowledgeDocument";
import "./Knowledge.css";

/* ---------------------------------------------------------------------------
   Knowledge -- one context product version's stored OKF bundle, inside the
   Context Products screen (R11-OKF02).

   Not an "OKF administration" screen, and not a route: the design says the
   knowledge view lives "inside existing Catalog object details and Context
   Products", so this is a panel the product row opens, beside Rollout and the
   compiler. Four things, the four the design names:

     readable document   the bundle's documents, grouped, opened in place --
                         and every bundle link inside a document opens its
                         target here too, which is the wiki reading
     coverage            the manifest's counts and the publication's own:
                         how many documents were rendered this time and how
                         many were carried unchanged
     version changes     the reader's lineage of publications, newest first,
                         with what each changed
     download            the archive of exactly the publication on screen

   PINNING. Every document read and the download name the publication the
   manifest described. A rebuild that lands while someone is reading therefore
   cannot splice a newer document into the older bundle they are looking at;
   "Refresh" is how they move to the newer one, and it says so.
--------------------------------------------------------------------------- */

type Group = { label: string; test: (path: string) => boolean };

const GROUPS: readonly Group[] = [
  { label: "Bundle", test: (p) => p === "index.md" || p === "log.md" },
  { label: "Tables", test: (p) => p.includes("/tables/") },
  { label: "Views", test: (p) => p.includes("/views/") },
  { label: "Routines", test: (p) => p.includes("/routines/") || p.includes("/packages/") },
  { label: "Concepts", test: (p) => p.startsWith("concepts/") },
  { label: "Tools", test: (p) => p.startsWith("tools/") },
  { label: "Indexes and logs", test: () => true },
];

function grouped(paths: readonly string[]): { label: string; paths: string[] }[] {
  const taken = new Set<string>();
  return GROUPS.map((group) => {
    const members = paths.filter((p) => !taken.has(p) && group.test(p));
    members.forEach((p) => taken.add(p));
    return { label: group.label, paths: members };
  }).filter((group) => group.paths.length > 0);
}

/** A short, human label for an opaque bundle path. The path stays the identity.
 *
 *  `names` maps an identity key to the name the manifest's value-free
 *  `source_objects` records for it, so a table reads as `bank.sales.orders`
 *  rather than as its digest. Anything the manifest does not name keeps a short
 *  form of its key, which is still unambiguous. */
export function pathLabel(path: string, names: ReadonlyMap<string, string> = new Map()): string {
  const segments = path.split("/");
  const leaf = segments[segments.length - 1] ?? path;
  const keyOf = (segment: string) => segment.match(/-([0-9a-f]{32})(?:\.md)?$/)?.[1] ?? null;
  if (leaf === "index.md" || leaf === "log.md") {
    const parent = segments[segments.length - 2];
    const kind = leaf === "index.md" ? "index" : "history";
    if (!parent) return leaf === "index.md" ? "bundle index" : "refresh history";
    const key = keyOf(parent);
    return `${parent.replace(/-[0-9a-f]{32}$/, "")} ${kind}${key ? ` ${key.slice(0, 8)}` : ""}`;
  }
  const key = keyOf(leaf);
  if (key && names.has(key)) return names.get(key) ?? leaf;
  return leaf.replace(/\.md$/, "").replace(/-([0-9a-f]{8})[0-9a-f]{24}$/, " $1");
}

function manifestNames(manifest: Record<string, unknown> | undefined): Map<string, string> {
  const rows = (manifest?.source_objects ?? []) as Array<Record<string, unknown>>;
  const names = new Map<string, string>();
  for (const row of rows) {
    if (typeof row.key === "string" && typeof row.qualified_name === "string") {
      names.set(row.key, row.qualified_name);
    }
  }
  return names;
}

async function readable<T>(load: () => Promise<T>): Promise<T> {
  try {
    return await load();
  } catch (reason) {
    if ((reason as Error)?.name === "AbortError") throw reason;
    throw new Error(describeKnowledgeError(reason));
  }
}

function TriggerPill({ trigger }: { trigger: string }) {
  const tone = trigger === "INITIAL" ? "info" : trigger === "SOURCE_CHANGE" ? "warn" : "mute";
  return <Pill tone={tone}>{trigger.toLowerCase().replace(/_/g, " ")}</Pill>;
}

function PublicationEntry({
  publication,
  onOpen,
  names,
}: {
  publication: OkfPublicationRead;
  onOpen: ((path: string) => void) | null;
  names: ReadonlyMap<string, string>;
}) {
  const { changes } = publication;
  const listed = [
    ...changes.changed.map((path) => ({ path, verb: "changed" })),
    ...changes.added.map((path) => ({ path, verb: "added" })),
    ...changes.removed.map((path) => ({ path, verb: "removed" })),
  ];
  return (
    <li className={`kview__pub${publication.is_current ? " kview__pub--current" : ""}`}>
      <div className="kview__pubhead">
        <strong>Publication {publication.sequence}</strong>
        <TriggerPill trigger={publication.trigger} />
        {publication.is_current ? <Pill tone="ok">current</Pill> : null}
        <span>{new Date(publication.captured_at).toLocaleString()}</span>
      </div>
      <div>
        {publication.trigger === "INITIAL"
          ? `${publication.document_count} documents first published.`
          : `${changes.changed.length} changed, ${changes.added.length} added, ${changes.removed.length} removed; ` +
            `${publication.rendered_count} rendered, ${publication.carried_count} carried unchanged.`}
      </div>
      {listed.length > 0 && publication.trigger !== "INITIAL" ? (
        <ul className="kview__paths">
          {listed.slice(0, 12).map(({ path, verb }) => (
            <li key={`${verb}:${path}`}>
              {onOpen && verb !== "removed" ? (
                <button type="button" onClick={() => onOpen(path)} title={path}>
                  {verb}: {pathLabel(path, names)}
                </button>
              ) : (
                <span title={path}>
                  {verb}: {pathLabel(path, names)}
                </span>
              )}
            </li>
          ))}
          {listed.length > 12 ? <li className="kview__note">and {listed.length - 12} more</li> : null}
        </ul>
      ) : null}
    </li>
  );
}

export function KnowledgeView({
  versionId,
  title,
  onClose,
}: {
  versionId: string;
  title: string;
  onClose: () => void;
}) {
  const bundle = useAsyncResource<OkfBundleRead>(
    (signal) => readable(() => fetchOkfBundle(versionId, signal)),
    [versionId],
  );
  const history = useAsyncResource<OkfPublicationHistoryRead>(
    (signal) => readable(() => fetchOkfPublications(versionId, signal)),
    [versionId],
  );
  const publicationId = bundle.data?.publication.publication_id ?? null;
  const [path, setPath] = useState<string>("index.md");
  const doc = useAsyncResource<OkfDocumentRead>(
    (signal) => readable(() => fetchOkfDocument(versionId, path, publicationId, signal)),
    [versionId, path, publicationId],
    { enabled: publicationId !== null },
  );
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  const paths = useMemo(() => (bundle.data?.files ?? []).map((file) => file.path), [bundle.data]);
  const known = useMemo(() => new Set(paths), [paths]);
  const names = useMemo(() => manifestNames(bundle.data?.manifest), [bundle.data]);
  const open = useCallback(
    (target: string) => {
      if (known.has(target)) setPath(target);
    },
    [known],
  );
  const reload = useCallback(() => {
    bundle.reload();
    history.reload();
  }, [bundle, history]);

  const download = useCallback(async () => {
    if (!publicationId) return;
    setDownloading(true);
    setDownloadError(null);
    try {
      await downloadOkfBundle(versionId, publicationId);
    } catch (reason) {
      setDownloadError(describeKnowledgeError(reason));
    } finally {
      setDownloading(false);
    }
  }, [versionId, publicationId]);

  const counts = (bundle.data?.manifest?.counts ?? {}) as Record<string, number>;
  const publication = bundle.data?.publication;

  return (
    <article className="kview" aria-label={`Knowledge for ${title}`}>
      <header className="kview__head">
        <div>
          <p className="kview__eyebrow">KNOWLEDGE</p>
          <h2 className="kview__h2">{title}</h2>
          <p className="kview__lede">
            The stored, approved knowledge bundle this version publishes to agents -- the same
            publication REST and MCP serve. Only what you are authorized to read is here, and
            nothing outside it is counted.
          </p>
        </div>
        <div className="kview__actions">
          <Button onClick={reload} title="Read the current publication; a rebuild may have landed">
            Refresh
          </Button>
          <Button
            variant="primary"
            onClick={() => void download()}
            disabled={!publication || !bundle.data?.valid || downloading}
            title={
              bundle.data && !bundle.data.valid
                ? "This bundle does not satisfy the publish policy, so it cannot be downloaded"
                : "Download exactly the publication on screen"
            }
          >
            {downloading ? "Preparing…" : "Download bundle"}
          </Button>
          <Button onClick={onClose}>Close</Button>
        </div>
      </header>

      {downloadError ? (
        <p className="kview__error" role="alert">
          {downloadError}
        </p>
      ) : null}

      {bundle.loading ? (
        <LoadingPanel label="Reading the stored knowledge bundle…" />
      ) : bundle.error ? (
        <p className="kview__error" role="alert">
          {bundle.error}
        </p>
      ) : bundle.data && publication ? (
        <>
          <div className="kview__stats" aria-label="Coverage">
            <span>
              Publication <b>{publication.sequence}</b>
            </span>
            <span>
              <b>{counts.tables ?? 0}</b> tables, <b>{counts.views ?? 0}</b> views,{" "}
              <b>{counts.routines ?? 0}</b> routines
            </span>
            <span>
              <b>{counts.concepts ?? 0}</b> concepts, <b>{counts.tools ?? 0}</b> tools
            </span>
            <span>
              <b>{counts.sources ?? 0}</b> sources
            </span>
            <span>
              <b>{publication.document_count}</b> documents ({publication.rendered_count} rendered,{" "}
              {publication.carried_count} carried unchanged)
            </span>
            {bundle.data.valid ? (
              <Pill tone="ok">publishable</Pill>
            ) : (
              <Pill tone="bad">{`${bundle.data.findings.length} policy finding(s)`}</Pill>
            )}
          </div>
          <div className="kview__digest">
            bundle {bundle.data.bundle_content_digest.slice(0, 16)} · checked current{" "}
            {new Date(bundle.data.validated_at).toLocaleString()} · spec {bundle.data.spec_revision.slice(0, 8)} (
            {bundle.data.spec_conformance.toLowerCase().replace(/_/g, " ")})
          </div>

          <div className="kview__cols">
            <nav aria-label="Bundle documents">
              <p className="kview__sub">Documents</p>
              {grouped(paths).map((group) => (
                <div key={group.label}>
                  <div className="kview__group">{group.label}</div>
                  <ul className="kview__files">
                    {group.paths.map((p) => (
                      <li key={p}>
                        <button
                          type="button"
                          className="kview__file"
                          aria-current={p === path ? "true" : undefined}
                          onClick={() => setPath(p)}
                          title={p}
                        >
                          <span>{pathLabel(p, names)}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </nav>
            <section className="kview__reader" aria-label="Document">
              {doc.loading ? (
                <LoadingPanel label="Opening document…" />
              ) : doc.error ? (
                <p className="kview__error" role="alert">
                  {doc.error}
                </p>
              ) : doc.data ? (
                <>
                  <div className="kview__docmeta">
                    <code>{doc.data.path}</code>
                    <span>sha256 {doc.data.sha256.slice(0, 12)}</span>
                    <span>
                      {doc.data.rendered_in_sequence === doc.data.publication_sequence
                        ? `changed in publication ${doc.data.rendered_in_sequence}`
                        : `unchanged since publication ${doc.data.rendered_in_sequence}`}
                    </span>
                  </div>
                  <KnowledgeDocument content={doc.data.content} path={doc.data.path} onNavigate={open} />
                </>
              ) : (
                <Empty title="Pick a document" hint="Links inside a document open their target here." />
              )}
            </section>
          </div>

          <section aria-label="Version changes">
            <p className="kview__sub">Version changes</p>
            {history.loading ? (
              <LoadingPanel label="Reading publication history…" />
            ) : history.error ? (
              <p className="kview__error" role="alert">
                {history.error}
              </p>
            ) : (
              <ul className="kview__history">
                {(history.data?.items ?? []).map((item) => (
                  <PublicationEntry
                    key={item.publication_id}
                    publication={item}
                    names={names}
                    onOpen={item.publication_id === publicationId ? open : null}
                  />
                ))}
              </ul>
            )}
            <p className="kview__note">
              A downloaded bundle cannot be recalled. It carries its publication id and digests;
              what you download is the publication shown here.
            </p>
          </section>
        </>
      ) : null}
    </article>
  );
}
