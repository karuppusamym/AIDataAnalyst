import { BusinessGeneration } from "../components/SemanticAuthor";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { MetadataBusinessAnnotationRead } from "../lib/types";
import { ApiError, fetchBusinessAnnotations, fetchBusinessMap, fetchTableBusinessAnnotation, fetchCatalogRows } from "../lib/api";
import {
  createGlossaryTerm,
  linkTermToTable,
  listGlossaryTerms,
  submitGlossaryTermVersion,
  type GlossaryTermRead,
} from "../lib/_api_append";
import { useUrlState } from "../lib/useUrlState";
import { useDatasourcePicker, datasourceName } from "../lib/useDatasourcePicker";
import { VirtualList } from "../components/VirtualList";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import "../components/EvidencePane.css";
import "./BusinessMeaningScreen.css";

/* ---------------------------------------------------------------------------
   Business meaning — UX-15/UX-16, the Catalog pattern against real,
   already-merged business-semantics routes (`semantic_intelligence_api.py`):

     - PRIMARY: `GET /v1/datasources/{id}/business-annotations`
       (`list_business_annotations`) — a datasource-scoped browse of every
       table with an *approved* business annotation, joining the current
       (AT-6, append-only-versioned) `MetadataBusinessAnnotationVersion` to
       its table/schema/domain/entity. Offset/limit paged (the route has no
       cursor and no server-side `q`), so the search box below filters the
       already-loaded page client-side, same as `CatalogScreen`'s filters
       narrow a loaded page but driven entirely client-side rather than by a
       query param the endpoint doesn't accept.

     - Evidence-style panel: `GET /v1/metadata/tables/{id}/business-annotation`
       (`get_table_business_annotation`) — resolves by table id alone,
       decoupled from whichever page is currently loaded, exactly the
       permalink contract `EvidencePane` already established for
       `fetchAssetEvidence` (UX-7). The list response already carries every
       field this panel shows, so the row from the list is shown immediately
       as progressive enhancement while this call resolves — but the panel
       never *requires* the row to be present, which is what makes
       `?ds=...&asset=...` a durable link rather than one that only works for
       whoever's loaded page happens to contain that table.

     - SECONDARY, scoped-in supporting view: `GET
       /v1/organizations/{id}/business-map` (`get_business_map`) — the real
       org-wide domain → entity → table taxonomy, cross-domain edges included
       (derived from actual `MetadataConstraint` foreign keys, not invented).
       Same "primary narrative, graph as a supporting tab" split
       `NarratedLineageScreen` (UX-20) established for lineage. Rendered here
       as a grouped tree rather than a canvas — a full graph-canvas renderer
       is LN-8's territory (Cytoscape, in `ui/`), not reimplemented here.
--------------------------------------------------------------------------- */

import { useOrgId } from "../lib/org";
const PAGE_LIMIT = 100;

function matchesQuery(a: MetadataBusinessAnnotationRead, q: string): boolean {
  if (!q.trim()) return true;
  const needle = q.trim().toLowerCase();
  const hay = `${a.table_name} ${a.schema_name} ${a.domain_name} ${a.entity_name} ${a.business_name}`.toLowerCase();
  return hay.includes(needle);
}

function AnnotationRow({
  annotation,
  selected,
  onSelect,
}: {
  annotation: MetadataBusinessAnnotationRead;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      className={`bmrow${selected ? " bmrow--selected" : ""}`}
      onClick={onSelect}
      aria-current={selected ? "true" : undefined}
    >
      <div className="bmrow__top">
        <span className="bmrow__name">{annotation.business_name}</span>
        <span className="bmrow__path">{annotation.schema_name}.{annotation.table_name}</span>
      </div>
      <p className="bmrow__desc">{annotation.business_description}</p>
      <div className="bmrow__badges">
        <Pill tone="accent">{annotation.domain_name}</Pill>
        <Pill tone="info">{annotation.entity_name}</Pill>
        <Pill tone="mute">{annotation.table_role.toLowerCase().replace(/_/g, " ")}</Pill>
      </div>
    </button>
  );
}

/** The evidence-style detail panel — same shape as `EvidencePane.tsx`
 *  (permalinkable by id, `row` is optional progressive enhancement, the
 *  fetch that actually resolves it is decoupled from the loaded list). Kept
 *  local to this screen rather than folded into `EvidencePane` itself: the
 *  fields are business-annotation-specific (domain/entity/grain/synonyms),
 *  not the generic evidence-item list `EvidencePane` renders, so the two
 *  share the `.evp` CSS shape (imported above) without sharing markup. */
function BusinessAnnotationPane({
  tableId,
  dsId,
  row,
  onClose,
}: {
  tableId: string | null;
  /** The currently selected datasource, folded into the permalink so a
   *  shared `?ds=...&asset=...` link reopens the same list, not just the
   *  panel. Cosmetic to resolution itself -- the fetch below only needs
   *  `tableId`. */
  dsId: string | null;
  row?: MetadataBusinessAnnotationRead | null;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<MetadataBusinessAnnotationRead | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!tableId) {
      setDetail(null);
      setError(null);
      return;
    }
    const ac = new AbortController();
    setDetail(null);
    setError(null);
    fetchTableBusinessAnnotation(tableId, ac.signal)
      .then(setDetail)
      .catch((e: unknown) => {
        if ((e as Error)?.name === "AbortError") return;
        setDetail(null);
        setError(e as Error);
      });
    return () => ac.abort();
  }, [tableId]);

  useEffect(() => setCopied(false), [tableId]);

  if (!tableId) {
    return (
      <aside className="evp evp--idle" aria-label="Business meaning">
        <Empty
          title="Select a table"
          hint="Its approved business meaning — domain, entity, description and grain — appears here."
        />
      </aside>
    );
  }

  const display = detail ?? row ?? null;
  const permalink = dsId
    ? `${location.origin}${location.pathname}?ds=${dsId}&asset=${tableId}`
    : `${location.origin}${location.pathname}?asset=${tableId}`;
  const displayName = display?.business_name ?? row?.business_name ?? tableId;

  return (
    <aside className="evp" aria-label={`Business meaning for ${displayName}`}>
      <header className="evp__head">
        <div className="evp__title">
          <div className="evp__name" title={displayName}>{displayName}</div>
          <div className="evp__path">
            {display ? `${display.schema_name}.${display.table_name}` : "Opened from a permalink"}
          </div>
        </div>
        <button className="evp__x" onClick={onClose} aria-label="Close business meaning">×</button>
      </header>

      <div className="evp__body">
        {error ? (
          <div className="evp__error" role="alert">
            {error instanceof ApiError && error.status === 404
              ? "No approved business annotation exists for this table."
              : error instanceof ApiError && error.status === 403
                ? "You are not authorized to view this table's business meaning."
                : `Business meaning could not be loaded: ${
                    error instanceof ApiError ? error.detail : error.message
                  }`}
          </div>
        ) : display === null ? (
          <div className="evp__load" role="status">Loading business meaning…</div>
        ) : (
          <>
            <div className="evp__terms">
              <div className="evp__pills">
                <Pill tone="accent">{display.domain_name}</Pill>
                <Pill tone="info">{display.entity_name}</Pill>
                <Pill tone="mute">{display.table_role.toLowerCase().replace(/_/g, " ")}</Pill>
              </div>
            </div>

            <dl className="bmdl">
              <dt>Business description</dt>
              <dd>{display.business_description}</dd>
              <dt>Grain</dt>
              <dd>{display.grain_statement}</dd>
              {display.synonyms.length > 0 ? (
                <>
                  <dt>Synonyms</dt>
                  <dd>{display.synonyms.join(", ")}</dd>
                </>
              ) : null}
            </dl>

            {display.suggested_questions.length > 0 ? (
              <div className="bmquestions">
                <div className="evp__sub">Suggested questions</div>
                <ul>
                  {display.suggested_questions.map((q, i) => (
                    <li key={i}>{q}</li>
                  ))}
                </ul>
              </div>
            ) : null}

            {display.tags.length > 0 ? (
              <div className="evp__terms">
                <div className="evp__sub">Tags</div>
                <div className="evp__pills">
                  {display.tags.map((t, i) => (
                    <Pill key={`${t}-${i}`} tone="mute">{t}</Pill>
                  ))}
                </div>
              </div>
            ) : null}

            <p className="bmapproved">
              Approved by {display.approved_by} · {display.approved_at.slice(0, 10)} · v{display.version}
            </p>
          </>
        )}
      </div>

      <footer className="evp__foot">
        <Button
          onClick={() => {
            void navigator.clipboard?.writeText(permalink);
            setCopied(true);
          }}
        >
          {copied ? "Link copied" : "Copy business-meaning link"}
        </Button>
        <span className="evp__hint">Permission-aware · AT-6 approved version</span>
      </footer>
    </aside>
  );
}

/** P1-03: Glossary tab -- lists glossary terms scoped to the current
 *  business node, backed by `GET /organizations/{org}/glossary-terms`.
 *  Client-side filter for the business-node id (the endpoint has no
 *  server-side filter for it yet -- see `listGlossaryTerms` for the
 *  rationale). "Create term" opens a form dialog that calls
 *  `createGlossaryTerm` + `submitGlossaryTermVersion`; "Link to asset"
 *  opens a search dialog that calls `linkTermToTable`. */
function GlossaryTab({
  organizationId,
  businessNodeId,
}: {
  organizationId: string;
  businessNodeId: string | null;
}) {
  const [terms, setTerms] = useState<GlossaryTermRead[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [creating, setCreating] = useState(false);
  const [linkingTerm, setLinkingTerm] = useState<GlossaryTermRead | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await listGlossaryTerms(organizationId, {
        businessNodeId: businessNodeId ?? undefined,
        limit: 200,
      });
      setTerms(page.items);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [organizationId, businessNodeId]);

  useEffect(() => {
    void load();
  }, [load]);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return terms;
    return terms.filter(
      (t) =>
        t.display_name.toLowerCase().includes(needle) ||
        t.term_key.toLowerCase().includes(needle) ||
        t.definition.toLowerCase().includes(needle),
    );
  }, [terms, q]);

  return (
    <div className="bmglossary">
      <div className="bmglossary__toolbar">
        <Field label="Search">
          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="term name, key or definition..."
          />
        </Field>
        <Button variant="primary" onClick={() => setCreating(true)}>
          Create term
        </Button>
      </div>

      {error ? (
        <ErrorState
          title="Glossary terms could not be loaded"
          detail={error}
          onRetry={() => void load()}
        />
      ) : loading ? (
        <div className="bmglossary__skeleton" role="status" aria-live="polite">
          Loading glossary terms...
        </div>
      ) : filtered.length === 0 ? (
        <Empty
          title={terms.length === 0 ? "No glossary terms yet" : "No matches"}
          hint={
            terms.length === 0
              ? "Create the first term with the button above."
              : "Try a different name or key."
          }
        />
      ) : (
        <table className="bmglossary__table" aria-label="Glossary terms">
          <thead>
            <tr>
              <th scope="col">Term</th>
              <th scope="col">Definition</th>
              <th scope="col">Status</th>
              <th scope="col" className="tnum">Synonyms</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((t) => (
              <tr key={t.id}>
                <td>
                  <div className="bmglossary__name">{t.display_name}</div>
                  <div className="bmglossary__key">{t.term_key}</div>
                </td>
                <td className="bmglossary__def" title={t.definition}>
                  {t.definition.length > 140
                    ? `${t.definition.slice(0, 140)}...`
                    : t.definition}
                </td>
                <td>
                  <Pill
                    tone={
                      t.status === "APPROVED"
                        ? "ok"
                        : t.status === "REJECTED"
                          ? "bad"
                          : t.status === "REVIEW_REQUIRED"
                            ? "warn"
                            : "mute"
                    }
                  >
                    {t.status.toLowerCase().replace(/_/g, " ")}
                  </Pill>
                </td>
                <td className="tnum">{t.synonyms.length}</td>
                <td className="bmglossary__actions">
                  <Button
                    onClick={() => setLinkingTerm(t)}
                    disabled={t.status !== "APPROVED"}
                    title={
                      t.status !== "APPROVED"
                        ? "Only approved terms can be linked to assets"
                        : "Link this term to a table"
                    }
                  >
                    Link to asset...
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {creating ? (
        <CreateTermDialog
          organizationId={organizationId}
          businessNodeId={businessNodeId}
          onClose={() => setCreating(false)}
          onCreated={() => {
            setCreating(false);
            void load();
          }}
        />
      ) : null}

      {linkingTerm ? (
        <LinkTermDialog
          organizationId={organizationId}
          term={linkingTerm}
          onClose={() => setLinkingTerm(null)}
          onLinked={() => {
            setLinkingTerm(null);
            void load();
          }}
        />
      ) : null}
    </div>
  );
}

function CreateTermDialog({
  organizationId,
  businessNodeId,
  onClose,
  onCreated,
}: {
  organizationId: string;
  businessNodeId: string | null;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [displayName, setDisplayName] = useState("");
  const [termKey, setTermKey] = useState("");
  const [definition, setDefinition] = useState("");
  const [synonymsRaw, setSynonymsRaw] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = useCallback(async () => {
    setSubmitting(true);
    setError(null);
    try {
      const created = await createGlossaryTerm(organizationId, {
        term_key: termKey.trim(),
        display_name: displayName.trim(),
        definition: definition.trim(),
        business_node_id: businessNodeId ?? undefined,
        synonyms: synonymsRaw
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
      });
      // Auto-submit for review -- ADR-0001: models propose, humans decide;
      // a term that stays in DRAFT forever helps nobody, so the create
      // form leaves it in REVIEW_REQUIRED so a reviewer can see it.
      try {
        await submitGlossaryTermVersion(created.id);
      } catch {
        /* If the auto-submit fails, the term still exists in DRAFT and
         *  the reviewer can submit it manually from the term row. */
      }
      onCreated();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [
    organizationId,
    businessNodeId,
    termKey,
    displayName,
    definition,
    synonymsRaw,
    onCreated,
  ]);

  const canSubmit =
    !submitting &&
    displayName.trim().length >= 2 &&
    termKey.trim().length >= 2 &&
    definition.trim().length >= 10;

  return (
    <div className="bmdialog__backdrop" role="dialog" aria-modal="true" aria-label="Create glossary term">
      <div className="bmdialog">
        <header className="bmdialog__head">
          <h2 className="bmdialog__h2">Create glossary term</h2>
          <button className="bmdialog__x" onClick={onClose} aria-label="Close">
            {"×"}
          </button>
        </header>
        <div className="bmdialog__body">
          <Field label="Display name">
            <input
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="Monthly Recurring Revenue"
            />
          </Field>
          <Field label="Term key">
            <input
              value={termKey}
              onChange={(e) => setTermKey(e.target.value.toLowerCase())}
              placeholder="mrr"
            />
          </Field>
          <Field label="Definition">
            <textarea
              value={definition}
              onChange={(e) => setDefinition(e.target.value)}
              rows={4}
              placeholder="Recurring revenue normalized to a monthly cadence, excluding one-time fees."
            />
          </Field>
          <Field label="Synonyms (comma-separated, optional)">
            <input
              value={synonymsRaw}
              onChange={(e) => setSynonymsRaw(e.target.value)}
              placeholder="recurring revenue, monthly rev"
            />
          </Field>
          {error ? <p className="bmdialog__err" role="alert">{error}</p> : null}
        </div>
        <footer className="bmdialog__foot">
          <Button onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => void submit()} disabled={!canSubmit}>
            {submitting ? "Creating..." : "Create and submit for review"}
          </Button>
        </footer>
      </div>
    </div>
  );
}

function LinkTermDialog({
  organizationId,
  term,
  onClose,
  onLinked,
}: {
  organizationId: string;
  term: GlossaryTermRead;
  onClose: () => void;
  onLinked: () => void;
}) {
  const [q, setQ] = useState("");
  const [candidates, setCandidates] = useState<{ id: string; name: string; schema_name: string }[]>([]);
  const [searching, setSearching] = useState(false);
  const [selected, setSelected] = useState<{ id: string; name: string } | null>(null);
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const search = useCallback(async () => {
    if (!q.trim()) return;
    setSearching(true);
    setError(null);
    try {
      const page = await fetchCatalogRows({
        organizationId,
        q: q.trim(),
        objectType: "TABLE",
        limit: 25,
      });
      setCandidates(page.items.map((r) => ({ id: r.id, name: r.name, schema_name: r.schema_name })));
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setSearching(false);
    }
  }, [organizationId, q]);

  const submit = useCallback(async () => {
    if (!selected) return;
    setSubmitting(true);
    setError(null);
    try {
      await linkTermToTable(organizationId, selected.id, term.term_id, {
        reason: reason.trim() || undefined,
      });
      onLinked();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [organizationId, selected, term.term_id, reason, onLinked]);

  return (
    <div className="bmdialog__backdrop" role="dialog" aria-modal="true" aria-label={`Link ${term.display_name}`}>
      <div className="bmdialog">
        <header className="bmdialog__head">
          <h2 className="bmdialog__h2">
            Link {"“"}{term.display_name}{"”"} to an asset
          </h2>
          <button className="bmdialog__x" onClick={onClose} aria-label="Close">
            {"×"}
          </button>
        </header>
        <div className="bmdialog__body">
          <Field label="Search asset by name">
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void search();
                }
              }}
              placeholder="orders_raw"
            />
          </Field>
          <Button onClick={() => void search()} disabled={searching || !q.trim()}>
            {searching ? "Searching..." : "Search"}
          </Button>
          {candidates.length > 0 ? (
            <ul className="bmdialog__results" role="listbox" aria-label="Matching tables">
              {candidates.map((c) => (
                <li key={c.id}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={selected?.id === c.id}
                    className={`bmdialog__opt${selected?.id === c.id ? " bmdialog__opt--sel" : ""}`}
                    onClick={() => setSelected({ id: c.id, name: `${c.schema_name}.${c.name}` })}
                  >
                    {c.schema_name}.{c.name}
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          {selected ? (
            <Field label="Reason (optional)">
              <input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="why this term applies to this asset"
              />
            </Field>
          ) : null}
          {error ? <p className="bmdialog__err" role="alert">{error}</p> : null}
        </div>
        <footer className="bmdialog__foot">
          <Button onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          <Button
            variant="primary"
            onClick={() => void submit()}
            disabled={submitting || !selected}
          >
            {submitting ? "Linking..." : `Link to ${selected?.name ?? "..."}`}
          </Button>
        </footer>
      </div>
    </div>
  );
}

/** The business-map tab: a grouped domain → entity → table tree over the
 *  real org-wide `get_business_map` response, plus a plain list of the
 *  cross-domain edges (each one a real `MetadataConstraint` foreign key that
 *  crosses a `BusinessDomain` boundary). Not a canvas — see this file's
 *  banner for why. */
function BusinessMapTab({ organizationId }: { organizationId: string }) {
  const [map, setMap] = useState<Awaited<ReturnType<typeof fetchBusinessMap>> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await fetchBusinessMap({ organizationId });
      setMap(result);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [organizationId]);

  useEffect(() => {
    void load();
  }, [load]);

  if (error) return <ErrorState title="The business map could not be loaded" detail={error} onRetry={() => void load()} />;
  if (loading || !map) {
    return (
      <div className="bmmap__skeleton" role="status" aria-live="polite">Loading business map…</div>
    );
  }

  const domains = map.nodes.filter((n) => n.node_type === "DOMAIN");
  const entitiesOf = (domainId: string) =>
    map.nodes.filter((n) => n.node_type === "ENTITY" && n.parent_id === domainId);
  const tablesOf = (entityId: string) =>
    map.nodes.filter((n) => n.node_type === "TABLE" && n.parent_id === entityId);
  const crossDomainEdges = map.edges.filter((e) => e.edge_type === "CROSS_DOMAIN_FOREIGN_KEY");

  if (domains.length === 0) {
    return <Empty title="No approved business domains yet" hint="The business map fills in as tables get an approved business annotation." />;
  }

  return (
    <div className="bmmap">
      <div className="bmmap__stats">
        <span><b className="tnum">{map.domain_count}</b> domains</span>
        <span><b className="tnum">{map.entity_count}</b> entities</span>
        <span><b className="tnum">{map.table_count}</b> tables</span>
        <span><b className="tnum">{map.cross_domain_edge_count}</b> cross-domain edges</span>
        {map.truncated ? <Pill tone="warn">truncated</Pill> : null}
      </div>

      <div className="bmmap__tree" role="tree" aria-label="Business domains, entities and tables">
        {domains.map((domain) => (
          <div key={domain.id} className="bmmap__domain" role="treeitem" aria-label={domain.label}>
            <div className="bmmap__domainhead">{domain.label}</div>
            {entitiesOf(domain.id).map((entity) => (
              <div key={entity.id} className="bmmap__entity">
                <div className="bmmap__entityhead">{entity.label}</div>
                <div className="bmmap__tables">
                  {tablesOf(entity.id).map((table) => (
                    <span key={table.id} className="bmmap__table" title={table.label}>{table.label}</span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        ))}
      </div>

      {crossDomainEdges.length > 0 ? (
        <div className="bmmap__cross">
          <div className="evp__sub">Cross-domain relationships</div>
          <ul>
            {crossDomainEdges.map((e) => {
              const source = map.nodes.find((n) => n.id === e.source_node_id);
              const target = map.nodes.find((n) => n.id === e.target_node_id);
              return (
                <li key={e.id}>
                  <b>{source?.label ?? e.source_node_id}</b> references <b>{target?.label ?? e.target_node_id}</b>
                  {" "}
                  <span className="bmmap__crossmeta">
                    ({String(e.evidence?.source_domain ?? "?")} → {String(e.evidence?.target_domain ?? "?")})
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

export function BusinessMeaningScreen() {
  const ORG = useOrgId();
  const [params, setParams] = useUrlState();
  const q = params.get("q") ?? "";
  const selectedId = params.get("asset");
  const viewParam = params.get("view");
  const view: "annotations" | "map" | "glossary" =
    viewParam === "map" ? "map" : viewParam === "glossary" ? "glossary" : "annotations";

  const { datasources, error: dsPickerError, preferredDatasourceId } = useDatasourcePicker(ORG);
  const dsId = params.get("ds") ?? preferredDatasourceId;
  const selectedDsName = datasourceName(datasources, dsId);

  const [items, setItems] = useState<MetadataBusinessAnnotationRead[]>([]);
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draftQ, setDraftQ] = useState(q);

  const inflight = useRef<AbortController | null>(null);
  const reqSeq = useRef(0);

  // Same "one in-flight request, sequence-guarded" shape as `CatalogScreen`.
  // Deliberately does NOT fetch when no datasource is selected: the endpoint
  // is scoped per datasource, so there is nothing to load until one is
  // picked -- an empty selector state, not a request that would 404 or
  // return an org-wide answer nobody asked for.
  const loadFirstPage = useCallback(async () => {
    inflight.current?.abort();
    if (!dsId) {
      setItems([]);
      setOffset(0);
      setTotal(null);
      setError(null);
      setLoading(false);
      return;
    }
    const ac = new AbortController();
    inflight.current = ac;
    const seq = ++reqSeq.current;

    setLoading(true);
    setError(null);
    try {
      const page = await fetchBusinessAnnotations(
        { datasourceId: dsId, limit: PAGE_LIMIT, offset: 0 },
        ac.signal,
      );
      if (seq !== reqSeq.current) return;
      setItems(page.items);
      setOffset(page.items.length);
      setTotal(page.total);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== reqSeq.current) return;
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === reqSeq.current) setLoading(false);
    }
  }, [dsId]);

  useEffect(() => {
    void loadFirstPage();
    return () => inflight.current?.abort();
  }, [loadFirstPage]);

  const loadMore = useCallback(async () => {
    if (!dsId || loadingMore || loading) return;
    if (total !== null && offset >= total) return;
    setLoadingMore(true);
    try {
      const page = await fetchBusinessAnnotations({ datasourceId: dsId, limit: PAGE_LIMIT, offset });
      setItems((prev) => [...prev, ...page.items]);
      setOffset((prev) => prev + page.items.length);
      setTotal(page.total);
    } catch {
      /* a failed next page leaves what is already loaded intact */
    } finally {
      setLoadingMore(false);
    }
  }, [dsId, offset, total, loading, loadingMore]);

  // Debounce typing, same as CatalogScreen -- filtering itself is client-side
  // (the endpoint takes no `q`), but the URL still holds the draft so a
  // filtered view stays shareable and doesn't re-render on every keystroke.
  useEffect(() => {
    const t = setTimeout(() => {
      if (draftQ !== q) setParams({ q: draftQ || null });
    }, 250);
    return () => clearTimeout(t);
  }, [draftQ, q, setParams]);

  const filtered = useMemo(() => items.filter((a) => matchesQuery(a, q)), [items, q]);

  const selected = useMemo(
    () => items.find((a) => a.table_id === selectedId) ?? null,
    [items, selectedId],
  );

  const hasMore = total !== null ? offset < total : false;

  return (
    <div className="bm">
      <header className="bm__head">
        <div>
          <h1 className="bm__h1">Business meaning</h1>
          <p className="bm__lede">
            Every table with an approved business annotation — domain, entity, description
            and grain, resolved to the current AT-6 approved version, not a model's proposal.
          </p>
        </div>
        {total !== null ? (
          <div className="bm__stats">
            <span><b className="tnum">{total}</b> annotated table{total === 1 ? "" : "s"}{selectedDsName ? ` in ${selectedDsName}` : ""}</span>
          </div>
        ) : null}
      </header>

      <div className="bm__filters">
        <Field label="Datasource">
          <select
            value={dsId ?? ""}
            onChange={(e) => setParams({ ds: e.target.value || null, asset: null })}
          >
            <option value="">Select a datasource…</option>
            {datasources.map((d) => (
              <option key={d.id} value={d.id}>{d.name}</option>
            ))}
          </select>
          {dsPickerError ? (
            <p className="bm__pickerr" role="alert">{dsPickerError}</p>
          ) : null}
        </Field>
        <Field label="Search">
          <input
            type="search"
            value={draftQ}
            placeholder="table, schema, domain or entity…"
            disabled={!dsId}
            onChange={(e) => setDraftQ(e.target.value)}
          />
        </Field>
      </div>

      <BusinessGeneration key={dsId ?? ORG} org={ORG} datasourceId={dsId} />
      <div className="bm__tabs" role="tablist">
        <button
          role="tab"
          aria-selected={view === "annotations"}
          className={`bm__tab${view === "annotations" ? " bm__tab--active" : ""}`}
          onClick={() => setParams({ view: null })}
        >
          Annotations
        </button>
        <button
          role="tab"
          aria-selected={view === "map"}
          className={`bm__tab${view === "map" ? " bm__tab--active" : ""}`}
          onClick={() => setParams({ view: "map" })}
        >
          Business map (supporting view)
        </button>
        <button
          role="tab"
          aria-selected={view === "glossary"}
          className={`bm__tab${view === "glossary" ? " bm__tab--active" : ""}`}
          onClick={() => setParams({ view: "glossary", asset: null })}
        >
          Glossary
        </button>
      </div>

      {view === "map" ? (
        <BusinessMapTab organizationId={ORG} />
      ) : view === "glossary" ? (
        <GlossaryTab organizationId={ORG} businessNodeId={params.get("node")} />
      ) : !dsId ? (
        <Empty
          title="Pick a datasource to see its business annotations"
          hint={dsPickerError ?? "Business annotations are scoped per datasource."}
        />
      ) : (
        <div className="bm__main">
          {error ? (
            <ErrorState
              title="Business annotations could not be loaded"
              detail={error}
              onRetry={() => void loadFirstPage()}
            />
          ) : loading ? (
            <div className="bm__skeleton" role="status" aria-live="polite">
              Loading business annotations…
            </div>
          ) : filtered.length === 0 ? (
            <Empty
              title={items.length === 0 ? "No approved business annotations yet" : "No matches"}
              hint={
                items.length === 0
                  ? "This datasource has no tables with an approved business annotation yet."
                  : "Try a different table, schema, domain or entity name."
              }
            />
          ) : (
            <VirtualList
              items={filtered}
              getKey={(a) => a.table_id}
              ariaLabel="Business annotations"
              estimateSize={130}
              totalCount={total}
              onReachEnd={hasMore ? () => void loadMore() : undefined}
              loadingMore={loadingMore}
              renderItem={(a) => (
                <AnnotationRow
                  annotation={a}
                  selected={a.table_id === selectedId}
                  onSelect={() => setParams({ asset: a.table_id })}
                />
              )}
            />
          )}

          <BusinessAnnotationPane
            tableId={selectedId}
            dsId={dsId}
            row={selected}
            onClose={() => setParams({ asset: null })}
          />
        </div>
      )}
    </div>
  );
}
