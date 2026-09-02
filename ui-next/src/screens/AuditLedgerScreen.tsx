import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AuditEventRead } from "../lib/ui-types";
import { ApiError, fetchAuditEvents } from "../lib/api";
import { useUrlState } from "../lib/useUrlState";
import { VirtualList } from "../components/VirtualList";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
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
--------------------------------------------------------------------------- */

const ORG = "00000000-0000-0000-0000-000000000001";
const nf = new Intl.NumberFormat("en-US");

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
  const [copied, setCopied] = useState(false);
  useEffect(() => setCopied(false), [event.id]);

  const permalink = `${location.origin}${location.pathname}?event=${event.id}`;
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
        <Button
          onClick={() => {
            void navigator.clipboard?.writeText(permalink);
            setCopied(true);
          }}
        >
          {copied ? "Link copied" : "Copy permalink"}
        </Button>
        <span className="evp__hint">UX-16 · org-wide</span>
      </footer>
    </aside>
  );
}

export function AuditLedgerScreen() {
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
  const [error, setError] = useState<string | null>(null);

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
    } catch {
      /* a failed next page leaves what is already loaded intact */
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, loading, items.length, total, action, resourceType, correlationId, since, until]);

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
        </div>
      </header>

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
