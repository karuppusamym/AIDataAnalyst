import { useCallback, useEffect, useRef, useState } from "react";
import type {
  CatalogBulkActionRunRead,
  CatalogBulkCertifyRequest,
  CatalogBulkClassifyRequest,
  CatalogBulkOwnRequest,
  CatalogBulkSelectionFilter,
  CatalogBulkTagRequest,
  UnownedAssetBacklogRouteResult,
  UnownedAssetEscalationRead,
} from "../lib/types";
import {
  ApiError,
  bulkAssignCatalogOwnership,
  bulkCertifyCatalogTables,
  bulkClassifyCatalogColumns,
  bulkTagCatalogTables,
  fetchUnownedAssetBacklog,
  routeUnownedAssetBacklog,
} from "../lib/api";
import { useOrgId } from "../lib/org";
import { useUrlState } from "../lib/useUrlState";
import { datasourceName, useDatasourcePicker } from "../lib/useDatasourcePicker";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "./StewardshipScreen.css";

/* ---------------------------------------------------------------------------
   Stewardship — catalog bulk actions (tag/classify/own/certify) and the
   unowned-asset stewardship backlog, ported from the legacy portal's single
   `#catalog-bulk-form` (one filter, four actions keyed off one
   `<select name="action">`) and its separate `#route-unowned` button
   (`ui/scripts/features/control-center.js`'s `renderCatalog`, roughly lines
   74-78 and 168-171).

   Real, already-merged endpoints this screen calls (`src/aida/api.py`,
   `src/aida/stewardship_api.py`):

     POST /v1/organizations/{id}/tables/bulk-tag       bulk_tag_tables
     POST /v1/organizations/{id}/tables/bulk-classify  bulk_classify_tables
     POST /v1/organizations/{id}/tables/bulk-own       bulk_own_tables
     POST /v1/organizations/{id}/tables/bulk-certify   bulk_certify_tables
     GET  /v1/organizations/{id}/stewardship/unowned-backlog
     POST /v1/organizations/{id}/stewardship/unowned-backlog/route

   Every bulk-* body carries exactly one of an explicit id list or `filter`
   (datasource + match field/pattern); the backend has no broader "match
   everything" mode, so — exactly like the legacy form — this screen is
   built around the filter path as its one, primary selection flow.

   Deliberately out of scope, stated rather than silently dropped:
     - Explicit `table_ids`/`column_ids` selection (e.g. picking specific
       rows out of a rendered catalog grid): the legacy form itself only
       ever built the `filter` path -- one datasource `<select>` plus one
       pattern input, never an id-list picker. `CatalogScreen` (owned by a
       different, currently-active process, out of this screen's scope) is
       the only place rows could be multi-selected from; this screen does
       not reach into it.
     - Resolving `table_id`/`subject_id` to a human-readable table or column
       name: neither `CatalogBulkActionItemRead` nor
       `UnownedAssetEscalationRead` carries one on the wire (no join back to
       `CatalogRowRead` from either endpoint), so ids are shown as-is
       (monospace) rather than invented display names.
     - `domain_id`/`line_of_business_id` scoping on "Route backlog": the
       route endpoint accepts them, but the legacy `#route-unowned` button
       itself only ever sent `datasource_id` (`data.get("datasource_id")`)
       -- this screen's one scope field matches that, not a second
       independent domain/LOB picker the legacy screen never had either.
     - Bulk *preview* (a dry-run match count before committing): neither
       endpoint has a preview mode -- `bulk-tag`/etc. always execute
       immediately server-side, so there is nothing to preview against.
--------------------------------------------------------------------------- */

const ACTION_VALUES = ["tag", "classify", "own", "certify"] as const;
type BulkActionType = (typeof ACTION_VALUES)[number];
const ACTION_LABELS: Record<BulkActionType, string> = {
  tag: "Tag tables",
  classify: "Classify columns",
  own: "Assign ownership",
  certify: "Certify tables",
};

const MATCH_FIELD_VALUES = ["TABLE_NAME", "SCHEMA_NAME", "QUALIFIED_NAME"] as const;
type MatchField = (typeof MATCH_FIELD_VALUES)[number];

const CLASSIFICATION_VALUES = [
  "UNCLASSIFIED", "PUBLIC", "INTERNAL", "CONFIDENTIAL", "PII", "PHI", "PCI", "SECRET",
] as const;
type Classification = (typeof CLASSIFICATION_VALUES)[number];

const BACKLOG_STATUS_VALUES = ["UNOWNED", "ROUTED", "ESCALATED", "ESCALATED_TIER_2", "RESOLVED"] as const;

const TAG_KEY_RE = /^[a-z][a-z0-9_-]{1,99}$/;

function humanize(s: string): string {
  return s.toLowerCase().replace(/_/g, " ");
}

const relTime = (iso: string | null): string => {
  if (!iso) return "never";
  const ms = Date.now() - new Date(iso).getTime();
  const min = Math.round(ms / 60_000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  return `${Math.round(hr / 24)}d ago`;
};

const backlogStatusTone = (status: string): Tone =>
  status === "RESOLVED" ? "ok" :
  status === "ESCALATED_TIER_2" ? "bad" :
  status === "ESCALATED" ? "warn" :
  status === "ROUTED" ? "info" :
  status === "UNOWNED" ? "warn" : "mute";

/** One year out, in the `<input type="datetime-local">` shape (local time,
 *  seconds truncated) -- the same convenience default the legacy form's
 *  `#catalog-bulk-form` certify expiry field ships with
 *  (`enhanceCompletedIngestionSurface`'s `certExpiry`). */
function defaultCertExpiry(): string {
  const d = new Date();
  d.setFullYear(d.getFullYear() + 1);
  const shifted = new Date(d.getTime() - d.getTimezoneOffset() * 60_000);
  return shifted.toISOString().slice(0, 16);
}

function BulkRunResult({ run }: { run: CatalogBulkActionRunRead }) {
  return (
    <div className="stew__result" aria-label="Bulk action result">
      <div className="stew__resulthead">
        <Pill tone="mute">{humanize(run.action)}</Pill>
        <span className="stew__resultid">{run.id}</span>
      </div>
      <div className="stew__resultcounts">
        <span>{run.requested_count} requested</span>
        <span className="stew__ok">{run.succeeded_count} succeeded</span>
        {run.failed_count > 0 ? <span className="stew__bad">{run.failed_count} failed</span> : null}
      </div>
      {run.results.length > 0 ? (
        <ul className="stew__resultitems">
          {run.results.map((item) => (
            <li key={item.subject_id} className="stew__resultitem">
              <Pill tone={item.status === "SUCCEEDED" ? "ok" : "bad"}>{item.status}</Pill>
              <code className="stew__resultsubject">{item.subject_id}</code>
              {item.reason ? <span className="stew__resultreason">{item.reason}</span> : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="stew__note">No subjects matched this filter.</p>
      )}
    </div>
  );
}

function RouteResultSummary({ result }: { result: UnownedAssetBacklogRouteResult }) {
  return (
    <div className="stew__routesummary" role="status" aria-label="Route backlog result">
      <span><strong>{result.routed.length}</strong> routed</span>
      <span><strong>{result.escalated.length}</strong> escalated</span>
      <span><strong>{result.escalated_tier2.length}</strong> escalated to tier 2</span>
      <span><strong>{result.resolved_count}</strong> resolved</span>
    </div>
  );
}

function BacklogRow({ row }: { row: UnownedAssetEscalationRead }) {
  return (
    <li className="stew__backrow">
      <div className="stew__backhead">
        <Pill tone={backlogStatusTone(row.status)}>{humanize(row.status)}</Pill>
        <code className="stew__backtable">{row.table_id}</code>
      </div>
      <div className="stew__backmeta">
        <span>first unowned {relTime(row.first_detected_unowned_at)}</span>
        {row.candidate_owner ? <span>candidate owner: {row.candidate_owner}</span> : null}
        {row.channel ? (
          <span>
            notify via {row.channel.toLowerCase()}
            {row.recipients.length > 0 ? ` (${row.recipients.join(", ")})` : ""}
          </span>
        ) : null}
        {row.resolved_at ? <span>resolved {relTime(row.resolved_at)}</span> : null}
      </div>
    </li>
  );
}

export function StewardshipScreen() {
  const ORG = useOrgId();
  const [params, setParams] = useUrlState();
  const { datasources, error: dsPickerError, preferredDatasourceId } = useDatasourcePicker(ORG);

  const actionParam = params.get("action");
  const action: BulkActionType = (ACTION_VALUES as readonly string[]).includes(actionParam ?? "")
    ? (actionParam as BulkActionType)
    : "tag";
  const fieldParam = params.get("field");
  const matchField: MatchField = (MATCH_FIELD_VALUES as readonly string[]).includes(fieldParam ?? "")
    ? (fieldParam as MatchField)
    : "TABLE_NAME";
  const matchPattern = params.get("pattern") ?? "";
  const datasourceId = params.get("ds") ?? preferredDatasourceId ?? "";

  const [tagKey, setTagKey] = useState("");
  const [tagValue, setTagValue] = useState("");
  const [columnNamePattern, setColumnNamePattern] = useState("*");
  const [classification, setClassification] = useState<Classification>("PII");
  const [ownerType, setOwnerType] = useState<"INDIVIDUAL" | "GROUP">("INDIVIDUAL");
  const [ownerPrincipal, setOwnerPrincipal] = useState("");
  const [rationale, setRationale] = useState("");
  const [expiresAt, setExpiresAt] = useState(defaultCertExpiry);

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [run, setRun] = useState<CatalogBulkActionRunRead | null>(null);

  const filterValid = Boolean(datasourceId) && matchPattern.trim().length > 0;
  const actionValid =
    action === "tag" ? TAG_KEY_RE.test(tagKey) :
    action === "own" ? ownerPrincipal.trim().length > 0 :
    action === "certify" ? rationale.trim().length >= 10 && new Date(expiresAt).getTime() > Date.now() :
    true; // classify: column_name_pattern defaults to "*", classification always has a selection
  const canSubmit = filterValid && actionValid && !submitting;

  const submit = useCallback(async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setSubmitError(null);
    const filter: CatalogBulkSelectionFilter = {
      datasource_id: datasourceId,
      match_field: matchField,
      match_pattern: matchPattern.trim(),
    };
    try {
      let result: CatalogBulkActionRunRead;
      if (action === "tag") {
        const body: CatalogBulkTagRequest = { filter, tag_key: tagKey, tag_value: tagValue.trim() || null };
        result = await bulkTagCatalogTables(ORG, body);
      } else if (action === "classify") {
        const body: CatalogBulkClassifyRequest = {
          filter,
          column_name_pattern: columnNamePattern.trim() || "*",
          classification,
        };
        result = await bulkClassifyCatalogColumns(ORG, body);
      } else if (action === "own") {
        const body: CatalogBulkOwnRequest = { filter, owner_type: ownerType, owner_principal: ownerPrincipal.trim() };
        result = await bulkAssignCatalogOwnership(ORG, body);
      } else {
        const body: CatalogBulkCertifyRequest = {
          filter,
          rationale: rationale.trim(),
          expires_at: new Date(expiresAt).toISOString(),
        };
        result = await bulkCertifyCatalogTables(ORG, body);
      }
      setRun(result);
    } catch (e) {
      setSubmitError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [
    canSubmit, action, ORG, datasourceId, matchField, matchPattern,
    tagKey, tagValue, columnNamePattern, classification, ownerType, ownerPrincipal, rationale, expiresAt,
  ]);

  // --- Unowned asset backlog -------------------------------------------------
  const [statusFilter, setStatusFilter] = useState("ALL");
  const [backlog, setBacklog] = useState<UnownedAssetEscalationRead[]>([]);
  const [backlogTotal, setBacklogTotal] = useState<number | null>(null);
  const [backlogLoading, setBacklogLoading] = useState(true);
  const [backlogError, setBacklogError] = useState<string | null>(null);
  const backlogInflight = useRef<AbortController | null>(null);

  const loadBacklog = useCallback(async () => {
    backlogInflight.current?.abort();
    const ac = new AbortController();
    backlogInflight.current = ac;
    setBacklogLoading(true);
    setBacklogError(null);
    try {
      const page = await fetchUnownedAssetBacklog(
        ORG,
        { status: statusFilter === "ALL" ? null : statusFilter, limit: 100 },
        ac.signal,
      );
      setBacklog(page.items);
      setBacklogTotal(page.total);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      setBacklogError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setBacklogLoading(false);
    }
  }, [ORG, statusFilter]);

  useEffect(() => {
    void loadBacklog();
    return () => backlogInflight.current?.abort();
  }, [loadBacklog]);

  const [routeScopeDatasourceId, setRouteScopeDatasourceId] = useState("");
  const [routing, setRouting] = useState(false);
  const [routeError, setRouteError] = useState<string | null>(null);
  const [routeResult, setRouteResult] = useState<UnownedAssetBacklogRouteResult | null>(null);

  const routeBacklog = useCallback(async () => {
    setRouting(true);
    setRouteError(null);
    setRouteResult(null);
    try {
      const result = await routeUnownedAssetBacklog(ORG, { datasource_id: routeScopeDatasourceId || null });
      setRouteResult(result);
      await loadBacklog();
    } catch (e) {
      setRouteError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setRouting(false);
    }
  }, [ORG, routeScopeDatasourceId, loadBacklog]);

  const dsLabel = datasourceName(datasources, datasourceId || null);

  return (
    <div className="stew">
      <header className="stew__head">
        <div>
          <h1 className="stew__h1">Stewardship</h1>
          <p className="stew__lede">
            Apply a catalog change to every table one filter matches, and route the backlog of
            tables the platform has detected have no assigned owner through escalation.
          </p>
        </div>
      </header>

      <div className="stew__grid">
        <section className="stew__panel" aria-label="Catalog bulk action">
          <div className="stew__panelhead">
            <p className="stew__eyebrow">CATALOG BULK ACTION</p>
            <h2 className="stew__h2">Tag, classify, own, or certify</h2>
          </div>

          <form
            className="stew__form"
            onSubmit={(e) => {
              e.preventDefault();
              void submit();
            }}
          >
            <Field label="Action">
              <select value={action} onChange={(e) => setParams({ action: e.target.value })}>
                {ACTION_VALUES.map((value) => (
                  <option key={value} value={value}>{ACTION_LABELS[value]}</option>
                ))}
              </select>
            </Field>

            <div className="stew__filterset">
              <Field label="Datasource">
                <select
                  value={datasourceId}
                  onChange={(e) => setParams({ ds: e.target.value || null })}
                  required
                >
                  <option value="">Select a datasource…</option>
                  {datasources.map((d) => (
                    <option key={d.id} value={d.id}>{d.name}</option>
                  ))}
                </select>
              </Field>
              <Field label="Match field">
                <select value={matchField} onChange={(e) => setParams({ field: e.target.value })}>
                  {MATCH_FIELD_VALUES.map((f) => (
                    <option key={f} value={f}>{humanize(f)}</option>
                  ))}
                </select>
              </Field>
              <Field label="Match pattern">
                <input
                  value={matchPattern}
                  onChange={(e) => setParams({ pattern: e.target.value || null })}
                  required
                  placeholder="raw_%"
                />
              </Field>
            </div>

            {action === "tag" ? (
              <div className="stew__actionfields">
                <Field label="Tag key">
                  <input
                    value={tagKey}
                    onChange={(e) => setTagKey(e.target.value)}
                    pattern="[a-z][a-z0-9_\-]{1,99}"
                    required
                    placeholder="pii-reviewed"
                  />
                </Field>
                <Field label="Tag value (optional)">
                  <input value={tagValue} onChange={(e) => setTagValue(e.target.value)} placeholder="true" />
                </Field>
              </div>
            ) : null}

            {action === "classify" ? (
              <div className="stew__actionfields">
                <Field label="Column name pattern">
                  <input
                    value={columnNamePattern}
                    onChange={(e) => setColumnNamePattern(e.target.value)}
                    placeholder="*"
                  />
                </Field>
                <Field label="Classification">
                  <select value={classification} onChange={(e) => setClassification(e.target.value as Classification)}>
                    {CLASSIFICATION_VALUES.map((c) => (
                      <option key={c} value={c}>{c}</option>
                    ))}
                  </select>
                </Field>
              </div>
            ) : null}

            {action === "own" ? (
              <div className="stew__actionfields">
                <Field label="Owner type">
                  <select value={ownerType} onChange={(e) => setOwnerType(e.target.value as "INDIVIDUAL" | "GROUP")}>
                    <option value="INDIVIDUAL">Individual</option>
                    <option value="GROUP">Group</option>
                  </select>
                </Field>
                <Field label="Owner principal">
                  <input
                    value={ownerPrincipal}
                    onChange={(e) => setOwnerPrincipal(e.target.value)}
                    required
                    placeholder="risk-data-stewards@tenant.example"
                  />
                </Field>
              </div>
            ) : null}

            {action === "certify" ? (
              <div className="stew__actionfields">
                <Field label="Rationale">
                  <input
                    value={rationale}
                    onChange={(e) => setRationale(e.target.value)}
                    minLength={10}
                    required
                    placeholder="Quarterly certification review completed."
                  />
                </Field>
                <Field label="Expires at">
                  <input
                    type="datetime-local"
                    value={expiresAt}
                    onChange={(e) => setExpiresAt(e.target.value)}
                    required
                  />
                </Field>
              </div>
            ) : null}

            {dsPickerError ? <p className="stew__err" role="alert">{dsPickerError}</p> : null}
            {submitError ? <p className="stew__err" role="alert">{submitError}</p> : null}

            <Button type="submit" variant="primary" disabled={!canSubmit}>
              {submitting ? "Running…" : `Run ${ACTION_LABELS[action].toLowerCase()}`}
            </Button>
          </form>

          {run ? <BulkRunResult run={run} /> : null}
        </section>

        <section className="stew__panel" aria-label="Unowned asset backlog">
          <div className="stew__panelhead">
            <div>
              <p className="stew__eyebrow">STEWARDSHIP BACKLOG</p>
              <h2 className="stew__h2">Unowned assets</h2>
            </div>
            {backlogTotal !== null ? <Pill tone="mute">{backlogTotal} total</Pill> : null}
          </div>

          <div className="stew__backlogcontrols">
            <Field label="Status">
              <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
                <option value="ALL">All statuses</option>
                {BACKLOG_STATUS_VALUES.map((s) => (
                  <option key={s} value={s}>{humanize(s)}</option>
                ))}
              </select>
            </Field>
            <Field label="Route scope (optional)">
              <select value={routeScopeDatasourceId} onChange={(e) => setRouteScopeDatasourceId(e.target.value)}>
                <option value="">Whole organization</option>
                {datasources.map((d) => (
                  <option key={d.id} value={d.id}>{d.name}</option>
                ))}
              </select>
            </Field>
            <Button variant="primary" disabled={routing} onClick={() => void routeBacklog()}>
              {routing ? "Routing…" : "Route backlog"}
            </Button>
          </div>

          {routeError ? <p className="stew__err" role="alert">{routeError}</p> : null}
          {routeResult ? <RouteResultSummary result={routeResult} /> : null}

          {backlogError ? (
            <ErrorState title="The unowned backlog could not be loaded" detail={backlogError} onRetry={() => void loadBacklog()} />
          ) : backlogLoading ? (
            <p className="stew__note">Loading…</p>
          ) : backlog.length === 0 ? (
            <Empty
              title="No unowned assets in this status"
              hint={dsLabel ? undefined : "Ownership coverage is clear for the current scope."}
            />
          ) : (
            <ul className="stew__backlist" aria-label="Unowned assets">
              {backlog.map((row) => (
                <BacklogRow key={row.id} row={row} />
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}
