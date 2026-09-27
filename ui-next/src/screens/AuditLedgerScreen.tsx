import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AuditEventRead } from "../lib/ui-types";
import { ApiError, describeLoadMoreFailure, downloadAuditEventsExport, fetchAuditEvents } from "../lib/api";
import { roleHolds } from "../lib/roles";
import { useSession } from "../lib/session";
import { useUrlState } from "../lib/useUrlState";
import { VirtualList } from "../components/VirtualList";
import { Button, CopyLinkButton, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import { failureText, StatusStrip, useStatusChannel } from "../components/screenState";
import "../components/EvidencePane.css";
import "./AuditLedgerScreen.css";

/* ---------------------------------------------------------------------------
   Audit ledger — UX-16, the Catalog pattern applied to the real
   `GET /v1/organizations/{organization_id}/audit-events` (`list_audit_events`,
   `operational_api.py:336`).

   Org-wide, unlike most of this shell's other screens: every other migrated
   screen scopes to one datasource, but an audit trail is meaningless scoped
   that way -- an action against a governance review or a marketplace request
   has no datasource at all -- so there is no datasource picker here.

   Same four pieces as `CatalogScreen`, adapted to this route's own contract:
     1. URL state       action / resource_type / correlation_id / since /
                         until / event (all five filters live in the URL, the
                         same reason the evidence pane is permalinkable)
     2. abortable fetch  one in-flight request, aborted on the next filter
                         change (`useUrlState`, shared -- see that module)
     3. virtualization   `VirtualList` -- an audit row is uniform-height, but
                         `CatalogTable`'s virtualization is hard-coded to
                         `CatalogRowRead`'s own seven columns; `VirtualList`
                         is the piece UX-15 already generalized for exactly
                         this shape of "list of records, one card each"
     4. evidence pane    the full event, including `details`, permalinkable
                         by `?event=<id>` -- mirrors `EvidencePane.tsx`'s
                         shape (permalink, close button, `.evp` chrome) but
                         resolves from the already-loaded page rather than a
                         second fetch: `list_audit_events` has no by-id GET,
                         so (honest gap, see this screen's own PR) a permalink
                         only resolves while the event is still in the
                         currently loaded window, the same caveat
                         `LineageRefusalScreen`'s `focusedDecision` carries.

   THIS route is `limit`/`offset` (`operational_api.py:336`), not the keyset
   `cursor` `fetchCatalogRows` uses -- `loadMore` below pages by
   `offset: items.length`, the same idiom `MarketplaceScreen`/
   `LineageRefusalScreen` already use against their own offset-paginated
   routes.

   EXPORT (R11-AUD08). Paging this browse until it runs out is not an export:
   nothing identifies the bytes and nothing records that the extraction
   happened. `GET .../audit-events/export.jsonl` (`audit_export_api.py`) is the
   surface for that -- one file, hashed, capped, and itself an audited event --
   so the header carries an Export action that sends the ledger's own filters to
   it. It is a deliberate click and never a load-time read, because each call
   writes `AUDIT_EVENTS_EXPORTED` to the ledger it exports.
--------------------------------------------------------------------------- */

import { useOrgId } from "../lib/org";
const nf = new Intl.NumberFormat("en-US");

/**
 * The roles `GET .../audit-events/export.jsonl` admits.
 *
 * Copied from the surface-control matrix row for
 * `aida.audit_export_api.export_audit_events` (action EXPORT,
 * `Docs/50-security/surface-control-matrix.md`): Auditor, Operations,
 * OrganizationAdmin, PlatformAdmin -- the same four the browse admits. Roles are
 * necessary, not sufficient: the server also runs the policy gate for EXPORT, so
 * a listed role can still be refused, and that refusal is shown as it arrives.
 */
const AUDIT_EXPORT_ROLES = ["Auditor", "Operations", "OrganizationAdmin", "PlatformAdmin"];

/** The refusal in the server's own words, with the one thing worth adding to a
 *  bare reason code: that being able to read the ledger is not the same
 *  permission as extracting it. */
function exportFailureText(reason: unknown): string {
  if (reason instanceof ApiError && reason.status === 403) {
    return `The audit export was refused: ${reason.detail}. Reading the ledger and exporting it are separate permissions.`;
  }
  return `The audit export failed: ${failureText(reason)}`;
}

const outcomeTone = (outcome: string): Tone =>
  outcome === "SUCCESS" ? "ok" : outcome === "DENIED" || outcome === "FAILURE" ? "bad" : "mute";

/** `<input type="datetime-local">` has no timezone of its own -- its value is
 *  always "local wall-clock time, no offset". `Date`'s constructor parses
 *  that as the *browser's* local zone, and `.toISOString()` always emits UTC
 *  with a trailing `Z` -- so round-tripping through `Date` is what turns a
 *  naive-looking picker value into the timezone-aware ISO string
 *  `fetchAuditEvents`/the server both require, rather than ever sending the
 *  naive string itself. */
function localInputToIso(value: string): string | null {
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

/** The inverse, so a `since`/`until` already in the URL (a UTC ISO string)
 *  redisplays in the picker as the equivalent local wall-clock value. */
function isoToLocalInput(iso: string): string {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function AuditRow({
  event,
  selected,
  onSelect,
}: {
  event: AuditEventRead;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <article className={`aevt${selected ? " aevt--sel" : ""}`} aria-label={event.action}>
      <button className="aevt__click" onClick={onSelect}>
        <div className="aevt__top">
          <span className="aevt__action">{event.action}</span>
          <Pill tone={outcomeTone(event.outcome)}>{event.outcome.toLowerCase()}</Pill>
        </div>
        <div className="aevt__resource">
          {event.resource_type}
          {event.resource_id ? <span className="aevt__rid">{event.resource_id}</span> : null}
        </div>
        <div className="aevt__meta">
          <span>{event.principal_id}</span>
          <span aria-hidden="true">·</span>
          <span>{event.principal_type.toLowerCase()}</span>
          <span aria-hidden="true">·</span>
          <time dateTime={event.occurred_at}>{event.occurred_at.slice(0, 19).replace("T", " ")}</time>
        </div>
      </button>
    </article>
  );
}

function EventDetailPane({ event, onClose }: { event: AuditEventRead; onClose: () => void }) {
  const detailEntries = Object.entries(event.details);
  // "Legible" means key/value rows when the shape allows it; a nested object
  // or array can't be flattened honestly, so it falls back to a pretty-printed
  // block rather than a lossy one-line-per-key rendering of something that
  // isn't actually flat.
  const isFlat = detailEntries.every(
    ([, v]) => v === null || ["string", "number", "boolean"].includes(typeof v),
  );

  return (
    <aside className="evp" aria-label={`Event ${event.id}`}>
      <header className="evp__head">
        <div className="evp__title">
          <div className="evp__name" title={event.action}>{event.action}</div>
          <div className="evp__path">{event.resource_type} · event {event.id}</div>
        </div>
        <button className="evp__x" onClick={onClose} aria-label="Close event detail">×</button>
      </header>

      <div className="evp__body">
        <ol className="evl">
          <li className={`evi ${event.outcome === "SUCCESS" ? "evi--ok" : "evi--bad"}`}>
            <div className="evi__label">Outcome</div>
            <div className="evi__value">{event.outcome}</div>
          </li>
          <li className="evi evi--info">
            <div className="evi__label">Principal</div>
            <div className="evi__value">{event.principal_id}</div>
            <div className="evi__source">{event.principal_type}</div>
          </li>
          <li className="evi evi--info">
            <div className="evi__label">Resource</div>
            <div className="evi__value">
              {event.resource_type}{event.resource_id ? ` · ${event.resource_id}` : ""}
            </div>
          </li>
          <li className="evi evi--info">
            <div className="evi__label">Correlation ID</div>
            <div className="evi__value aud__mono">{event.correlation_id}</div>
          </li>
          <li className="evi evi--info">
            <div className="evi__label">Source IP</div>
            <div className="evi__value">{event.source_ip ?? "—"}</div>
          </li>
          <li className="evi evi--info">
            <div className="evi__label">Occurred at</div>
            <div className="evi__value">
              <time dateTime={event.occurred_at}>{event.occurred_at}</time>
            </div>
          </li>
        </ol>

        <div className="evp__terms">
          <div className="evp__sub">Details</div>
          {detailEntries.length === 0 ? (
            <p className="aud__nodetails">No additional details recorded.</p>
          ) : isFlat ? (
            <dl className="aud__kv">
              {detailEntries.map(([k, v]) => (
                <div className="aud__kvrow" key={k}>
                  <dt>{k}</dt>
                  <dd>{v === null ? "—" : String(v)}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <pre className="aud__json">{JSON.stringify(event.details, null, 2)}</pre>
          )}
        </div>
      </div>

      <footer className="evp__foot">
{/* The copied link names the screen that resolves this selection.
            Built as `origin + pathname + '?' + id` it carried no `#/audit`,
            so a fresh tab landed on the persona default and the id was read by
            nobody (review 2026-09-05, F08). */}
        <CopyLinkButton
          target={{ screen: "audit", params: { event: event.id } }}
          label="Copy permalink"
        />
        <span className="evp__hint">UX-16 · org-wide</span>
      </footer>
    </aside>
  );
}

export function AuditLedgerScreen() {
  const ORG = useOrgId();
  const [params, setParams] = useUrlState();

  const action = params.get("action") ?? "";
  const resourceType = params.get("resource_type") ?? "";
  const correlationId = params.get("correlation_id") ?? "";
  const since = params.get("since") ?? "";
  const until = params.get("until") ?? "";
  const selectedEventId = params.get("event");

  const [draftAction, setDraftAction] = useState(action);
  const [draftResourceType, setDraftResourceType] = useState(resourceType);
  const [draftCorrelationId, setDraftCorrelationId] = useState(correlationId);

  const [items, setItems] = useState<AuditEventRead[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Known-only, unlike a read: bulk extraction of the ledger is never offered on
  // a guess, and `/v1/me` answers within a moment of the page opening.
  const mayExport = roleHolds(useSession().me?.roles, AUDIT_EXPORT_ROLES);
  const exportStatus = useStatusChannel();
  const [exporting, setExporting] = useState(false);
  const exportInflight = useRef<AbortController | null>(null);

  // One in-flight request at a time -- aborting the previous one is what
  // stops a slow first page from overwriting the results of a newer,
  // narrower filter (the same reason `CatalogScreen.loadFirstPage` does it).
  const inflight = useRef<AbortController | null>(null);
  const reqSeq = useRef(0);

  const loadFirstPage = useCallback(async () => {
    inflight.current?.abort();
    const ac = new AbortController();
    inflight.current = ac;
    const seq = ++reqSeq.current;

    setLoading(true);
    setError(null);
    try {
      const page = await fetchAuditEvents(
        {
          organizationId: ORG,
          action: action || undefined,
          resourceType: resourceType || undefined,
          correlationId: correlationId || undefined,
          since: since || undefined,
          until: until || undefined,
          limit: 100,
          offset: 0,
        },
        ac.signal,
      );
      if (seq !== reqSeq.current) return;
      setItems(page.items);
      setTotal(page.total);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      if (seq !== reqSeq.current) return;
      setError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      if (seq === reqSeq.current) setLoading(false);
    }
  }, [action, resourceType, correlationId, since, until]);

  useEffect(() => {
    void loadFirstPage();
    return () => inflight.current?.abort();
  }, [loadFirstPage]);

  const loadMore = useCallback(async () => {
    if (loadingMore || loading || items.length >= (total ?? 0)) return;
    setLoadingMore(true);
    try {
      const page = await fetchAuditEvents({
        organizationId: ORG,
        action: action || undefined,
        resourceType: resourceType || undefined,
        correlationId: correlationId || undefined,
        since: since || undefined,
        until: until || undefined,
        limit: 100,
        offset: items.length,
      });
      setItems((prev) => [...prev, ...page.items]);
      setLoadMoreError(null);
    } catch (e) {
      // What is already loaded still stands -- but saying nothing made "you
      // have reached the end" and "you were refused" look identical, which on
      // an audit ledger is the difference between a complete record and a
      // truncated one.
      setLoadMoreError(describeLoadMoreFailure(e));
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, loading, items.length, total, action, resourceType, correlationId, since, until]);

  /* The export sends the filters the LIST is showing -- the ones in the URL --
     and not the half-typed text in the boxes, which the debounce below has not
     yet committed: what is downloaded must be what is on screen. */
  const exportEvents = useCallback(async () => {
    if (exportInflight.current) return;
    const controller = new AbortController();
    exportInflight.current = controller;
    setExporting(true);
    exportStatus.info("Preparing the export. The server builds the whole file before it downloads.");
    try {
      const result = await downloadAuditEventsExport(
        {
          organizationId: ORG,
          action: action || undefined,
          resourceType: resourceType || undefined,
          correlationId: correlationId || undefined,
          since: since || undefined,
          until: until || undefined,
        },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      const events =
        result.rowCount === null
          ? "the audit events"
          : `${nf.format(result.rowCount)} event${result.rowCount === 1 ? "" : "s"}`;
      if (result.truncated === true) {
        // Visible or it is a lie (`audit_export_api.py`): a file cut at the cap
        // reads as a quiet period unless it says otherwise.
        exportStatus.failure(
          `Downloaded ${result.filename}, but it is INCOMPLETE: the export stopped at the server's limit` +
            `${result.rowLimit === null ? "" : ` of ${nf.format(result.rowLimit)} events`}. ` +
            "Narrow the time range with Since and Until and export again.",
        );
      } else if (result.truncated === null) {
        exportStatus.failure(
          `Downloaded ${events} as ${result.filename}, but the server's completeness flag could not be read, ` +
            "so it is not confirmed that nothing was cut off.",
        );
      } else {
        exportStatus.success(
          `Exported ${events} as ${result.filename}.` +
            `${result.sha256 ? ` SHA-256 ${result.sha256}.` : ""} The export was recorded in the ledger.`,
        );
      }
    } catch (reason) {
      if ((reason as Error)?.name === "AbortError") return;
      exportStatus.failure(exportFailureText(reason));
    } finally {
      if (exportInflight.current === controller) exportInflight.current = null;
      if (!controller.signal.aborted) setExporting(false);
    }
  }, [ORG, action, resourceType, correlationId, since, until, exportStatus]);

  // Leaving the screen drops an export that has not arrived.
  useEffect(() => () => exportInflight.current?.abort(), []);

  // Debounce the three free-text filters so each keystroke doesn't become a
  // request -- the same reason `CatalogScreen` debounces its search box.
  useEffect(() => {
    const t = setTimeout(() => {
      const patch: Record<string, string | null> = {};
      let changed = false;
      if (draftAction !== action) { patch.action = draftAction || null; changed = true; }
      if (draftResourceType !== resourceType) { patch.resource_type = draftResourceType || null; changed = true; }
      if (draftCorrelationId !== correlationId) { patch.correlation_id = draftCorrelationId || null; changed = true; }
      if (changed) setParams({ ...patch, event: null });
    }, 250);
    return () => clearTimeout(t);
  }, [draftAction, draftResourceType, draftCorrelationId, action, resourceType, correlationId, setParams]);

  const selected = useMemo(
    () => items.find((e) => String(e.id) === selectedEventId) ?? null,
    [items, selectedEventId],
  );

  return (
    <div className="aud">
      <header className="aud__head">
        <div>
          <h1 className="aud__h1">Audit ledger</h1>
          <p className="aud__lede">
            Every recorded action across the organization — who did what, to what, and
            whether it was allowed (UX-16, <code>list_audit_events</code>).
          </p>
        </div>
        <div className="aud__stats">
          <span><b className="tnum">{total !== null ? nf.format(total) : "—"}</b> events</span>
          {mayExport ? (
            <Button
              disabled={exporting}
              onClick={() => void exportEvents()}
              title="Download every event matching the filters below as JSON Lines. The export is itself recorded in the ledger."
            >
              {exporting ? "Exporting…" : "Export JSONL"}
            </Button>
          ) : null}
        </div>
      </header>

      <StatusStrip status={exportStatus.status} />

      <div className="aud__filters">
        <Field label="Action">
          <input
            type="text"
            value={draftAction}
            placeholder="e.g. governance_review.decide"
            onChange={(e) => setDraftAction(e.target.value)}
          />
        </Field>
        <Field label="Resource type">
          <input
            type="text"
            value={draftResourceType}
            placeholder="e.g. TABLE"
            onChange={(e) => setDraftResourceType(e.target.value)}
          />
        </Field>
        <Field label="Correlation ID">
          <input
            type="text"
            value={draftCorrelationId}
            placeholder="corr_…"
            onChange={(e) => setDraftCorrelationId(e.target.value)}
          />
        </Field>
        <Field label="Since">
          <input
            type="datetime-local"
            value={since ? isoToLocalInput(since) : ""}
            onChange={(e) => {
              const iso = localInputToIso(e.target.value);
              setParams({ since: iso, event: null });
            }}
          />
        </Field>
        <Field label="Until">
          <input
            type="datetime-local"
            value={until ? isoToLocalInput(until) : ""}
            onChange={(e) => {
              const iso = localInputToIso(e.target.value);
              setParams({ until: iso, event: null });
            }}
          />
        </Field>
        {action || resourceType || correlationId || since || until ? (
          <Button
            onClick={() => {
              setDraftAction("");
              setDraftResourceType("");
              setDraftCorrelationId("");
              setParams({
                action: null, resource_type: null, correlation_id: null,
                since: null, until: null, event: null,
              });
            }}
          >
            Clear filters
          </Button>
        ) : null}
      </div>

      <div className="aud__main">
        {error ? (
          <ErrorState title="The audit ledger could not be loaded" detail={error} onRetry={() => void loadFirstPage()} />
        ) : loading ? (
          <div className="aud__skeleton" role="status" aria-live="polite">
            Loading audit ledger…
          </div>
        ) : (
          <VirtualList
            items={items}
            getKey={(e) => String(e.id)}
            ariaLabel="Audit events"
            estimateSize={104}
            totalCount={total}
            onReachEnd={() => void loadMore()}
            loadingMore={loadingMore}
            loadMoreError={loadMoreError}
            emptyState={
              <Empty title="No audit events match these filters" hint="Try clearing a filter, such as the time range." />
            }
            renderItem={(e) => (
              <AuditRow
                event={e}
                selected={String(e.id) === selectedEventId}
                onSelect={() => setParams({ event: String(e.id) })}
              />
            )}
          />
        )}
        {selected ? (
          <EventDetailPane event={selected} onClose={() => setParams({ event: null })} />
        ) : null}
      </div>
    </div>
  );
}
