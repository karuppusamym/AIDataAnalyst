import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import { useUnsavedChanges } from "../lib/unsavedChanges";
import { datasourceName, useDatasourcePicker } from "../lib/useDatasourcePicker";
import { OwnershipExpiryBannerScreen } from "./OwnershipExpiryBannerScreen";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "./StewardshipScreen.css";

/* ---------------------------------------------------------------------------
   R11-S13 (items 15/17): this file is two VIEWS of the stewardship workspace
   now, not one screen. `StewardshipWorkQueue` is the unowned backlog plus the
   ownership-expiry banner; `StewardshipBulkActions` is the bulk form. They
   were two side-by-side panels of one page, and `StewardshipWorkspace.tsx`
   puts each behind its own `?view=` tab, next to Automation (Playbooks).
   The split moved code; it changed no endpoint, no request body and no
   control's enablement -- see the action map in
   `Docs/10-architecture/23-stewardship-action-map.md`.

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
   everything" mode, so — exactly like the legacy form — this screen was
   built around the filter path as its primary selection flow.

   EXPLICIT SELECTION (17B, 2026-09-19). `CatalogScreen` can now send a row
   selection here as `?ids=` -- a comma-separated `table_ids` list -- which
   this form sends instead of `filter`, never both (the four request shapes
   below make them mutually exclusive; `_require_exactly_one_selection` on
   the server rejects a body carrying both). No endpoint, role or request
   shape changed to add this: `table_ids` was already accepted by every
   bulk-* route, the same as the filter path, with the same audit trail. A
   steward who checks rows in Catalog and one who opens Bulk actions and
   types a pattern that happens to match the same rows run the identical
   code.

   Deliberately out of scope, stated rather than silently dropped:
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

/** What the bulk form asks before its typed values are discarded. */
export const BULK_UNSAVED_MESSAGE = "Discard the bulk action you have not run?";

/**
 * Stewardship → Bulk actions. One filter, four actions, applied on submit.
 *
 * The filter (`action`/`ds`/`field`/`pattern`) lives in the URL and survives a
 * tab switch; the action's own fields (tag key, owner, rationale…) are local
 * state and do not. Before the workspace, both panels shared one page, so
 * nothing on it could unmount the form. A tab switch now can, so the form
 * reports an edited, un-run action to `lib/unsavedChanges` -- the registry the
 * workspace's tab bar and the shell's navigation both ask.
 */
export function StewardshipBulkActions() {
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

  /* 17B: an explicit selection from Catalog's row checkboxes, carried as
     `?ids=` -- a comma-separated `table_ids` list -- rather than the
     `field`/`pattern` filter. Trimmed and emptied entries dropped so a
     stray comma in a hand-edited URL cannot become an empty-string id in
     the request body. */
  const explicitIds = useMemo(() => {
    const raw = params.get("ids");
    if (!raw) return [] as string[];
    return raw
      .split(",")
      .map((id) => id.trim())
      .filter(Boolean);
  }, [params]);
  const hasExplicitSelection = explicitIds.length > 0;
  const clearExplicitSelection = useCallback(() => setParams({ ids: null }), [setParams]);

  const [tagKey, setTagKey] = useState("");
  const [tagValue, setTagValue] = useState("");
  const [columnNamePattern, setColumnNamePattern] = useState("*");
  const [classification, setClassification] = useState<Classification>("PII");
  const [ownerType, setOwnerType] = useState<"INDIVIDUAL" | "GROUP">("INDIVIDUAL");
  const [ownerPrincipal, setOwnerPrincipal] = useState("");
  const [rationale, setRationale] = useState("");
  const [expiresAt, setExpiresAt] = useState(defaultCertExpiry);

  /* True from the first change to an action field until that action runs.
     The filter is deliberately not counted: it is in the URL, and neither a
     tab switch nor a reload loses it. A failed run leaves this set -- the
     values are still the user's unfinished work. */
  const [edited, setEdited] = useState(false);
  useUnsavedChanges(edited, BULK_UNSAVED_MESSAGE);
  const edit = useCallback(<T,>(set: (value: T) => void, value: T) => {
    set(value);
    setEdited(true);
  }, []);

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [run, setRun] = useState<CatalogBulkActionRunRead | null>(null);

  const filterValid = hasExplicitSelection || (Boolean(datasourceId) && matchPattern.trim().length > 0);
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
    /* 17B: `table_ids` and `filter` are mutually exclusive on every bulk-*
       body (the server's `_require_exactly_one_selection` rejects both), so
       an explicit Catalog selection replaces the filter entirely rather than
       riding alongside it. */
    const selection: { table_ids: string[] } | { filter: CatalogBulkSelectionFilter } = hasExplicitSelection
      ? { table_ids: explicitIds }
      : {
          filter: {
            datasource_id: datasourceId,
            match_field: matchField,
            match_pattern: matchPattern.trim(),
          },
        };
    try {
      let result: CatalogBulkActionRunRead;
      if (action === "tag") {
        const body: CatalogBulkTagRequest = { ...selection, tag_key: tagKey, tag_value: tagValue.trim() || null };
        result = await bulkTagCatalogTables(ORG, body);
      } else if (action === "classify") {
        const body: CatalogBulkClassifyRequest = {
          ...selection,
          column_name_pattern: columnNamePattern.trim() || "*",
          classification,
        };
        result = await bulkClassifyCatalogColumns(ORG, body);
      } else if (action === "own") {
        const body: CatalogBulkOwnRequest = { ...selection, owner_type: ownerType, owner_principal: ownerPrincipal.trim() };
        result = await bulkAssignCatalogOwnership(ORG, body);
      } else {
        const body: CatalogBulkCertifyRequest = {
          ...selection,
          rationale: rationale.trim(),
          expires_at: new Date(expiresAt).toISOString(),
        };
        result = await bulkCertifyCatalogTables(ORG, body);
      }
      setRun(result);
      setEdited(false);
    } catch (e) {
      setSubmitError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [
    canSubmit, action, ORG, hasExplicitSelection, explicitIds, datasourceId, matchField, matchPattern,
    tagKey, tagValue, columnNamePattern, classification, ownerType, ownerPrincipal, rationale, expiresAt,
  ]);

  return (
    <div className="stew">
      <header className="stew__head">
        <div>
          <h1 className="stew__h1">Bulk actions</h1>
          <p className="stew__lede">
            Apply one catalog change to every table one filter matches. A run applies as soon as
            it is submitted: none of these endpoints has a preview mode.
          </p>
        </div>
      </header>

      <div className="stew__body">
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

            {hasExplicitSelection ? (
              /* 17B: an explicit Catalog selection replaces the filter --
                 never alongside it, see `submit`'s mutually-exclusive
                 `selection`. "Use a filter instead" drops `?ids=`, which
                 re-reveals the filter fields below with whatever they last
                 held (the URL never dropped them, it just stopped being
                 read while a selection was in front). */
              <div className="stew__selection" role="status" aria-label="Explicit selection">
                <Pill tone="accent">
                  {explicitIds.length} table{explicitIds.length === 1 ? "" : "s"} selected in Catalog
                </Pill>
                <Button onClick={clearExplicitSelection}>Use a filter instead</Button>
              </div>
            ) : (
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
            )}

            {action === "tag" ? (
              <div className="stew__actionfields">
                <Field label="Tag key">
                  <input
                    value={tagKey}
                    onChange={(e) => edit(setTagKey, e.target.value)}
                    pattern="[a-z][a-z0-9_\-]{1,99}"
                    required
                    placeholder="pii-reviewed"
                  />
                </Field>
                <Field label="Tag value (optional)">
                  <input value={tagValue} onChange={(e) => edit(setTagValue, e.target.value)} placeholder="true" />
                </Field>
              </div>
            ) : null}

            {action === "classify" ? (
              <div className="stew__actionfields">
                <Field label="Column name pattern">
                  <input
                    value={columnNamePattern}
                    onChange={(e) => edit(setColumnNamePattern, e.target.value)}
                    placeholder="*"
                  />
                </Field>
                <Field label="Classification">
                  <select value={classification} onChange={(e) => edit(setClassification, e.target.value as Classification)}>
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
                  <select value={ownerType} onChange={(e) => edit(setOwnerType, e.target.value as "INDIVIDUAL" | "GROUP")}>
                    <option value="INDIVIDUAL">Individual</option>
                    <option value="GROUP">Group</option>
                  </select>
                </Field>
                <Field label="Owner principal">
                  <input
                    value={ownerPrincipal}
                    onChange={(e) => edit(setOwnerPrincipal, e.target.value)}
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
                    onChange={(e) => edit(setRationale, e.target.value)}
                    minLength={10}
                    required
                    placeholder="Quarterly certification review completed."
                  />
                </Field>
                <Field label="Expires at">
                  <input
                    type="datetime-local"
                    value={expiresAt}
                    onChange={(e) => edit(setExpiresAt, e.target.value)}
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
      </div>
    </div>
  );
}

/**
 * Stewardship → Work queue. The unowned-asset backlog and its routing run,
 * with the principal's own expiring ownerships above it.
 */
export function StewardshipWorkQueue() {
  const ORG = useOrgId();
  const [params] = useUrlState();
  const { datasources, preferredDatasourceId } = useDatasourcePicker(ORG);

  const [statusFilter, setStatusFilter] = useState("ALL");
  /* The advanced filter design 21 §17 asks the Work queue to carry, matching
     `NegativeKnowledgeScreen`'s own free-text "Assertion type" filter next to
     its categorical one. It used to narrow only the page already loaded,
     because `list_unowned_asset_backlog` (stewardship_api.py) took nothing but
     `status`; it takes `candidate_owner` now, so it is sent like Status is and
     the backlog is narrowed as a whole -- before paging, and `total` counts the
     matches. The server matches exactly and case-sensitively, so a value is
     applied when the steward asks for it (Enter, or the button) rather than on
     every keystroke, and the names already seen are offered as suggestions. */
  const [candidateOwnerDraft, setCandidateOwnerDraft] = useState("");
  const [candidateOwnerFilter, setCandidateOwnerFilter] = useState("");
  const [knownCandidateOwners, setKnownCandidateOwners] = useState<string[]>([]);
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
        {
          status: statusFilter === "ALL" ? null : statusFilter,
          limit: 100,
          ...(candidateOwnerFilter ? { candidateOwner: candidateOwnerFilter } : {}),
        },
        ac.signal,
      );
      setBacklog(page.items);
      setBacklogTotal(page.total);
      // What a steward can pick from is what they have seen, across filters: a page narrowed
      // to one owner would otherwise offer only that owner.
      setKnownCandidateOwners((known) => {
        const seen = new Set(known);
        for (const row of page.items) if (row.candidate_owner) seen.add(row.candidate_owner);
        return seen.size === known.length ? known : [...seen].sort();
      });
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      setBacklogError(e instanceof ApiError ? e.detail : (e as Error).message);
    } finally {
      setBacklogLoading(false);
    }
  }, [ORG, statusFilter, candidateOwnerFilter]);

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

  /* The empty-state hint read the bulk form's datasource when both panels
     shared a page. `ds` is still what that form reads, so the hint says the
     same thing it said before the split. */
  const dsLabel = datasourceName(datasources, params.get("ds") ?? preferredDatasourceId ?? null);

  return (
    <div className="stew">
      <header className="stew__head">
        <div>
          <h1 className="stew__h1">Work queue</h1>
          <p className="stew__lede">
            Tables the platform has detected have no assigned owner, routed through escalation —
            and any ownership of yours that is about to lapse.
          </p>
        </div>
      </header>

      {/* P2-07's banner. It was built as a standalone component for a shell to
          embed and then never embedded anywhere, so an owner was never warned
          before an ownership lapsed. Ownership is worked on in this queue, so
          it belongs at the top of it; it renders nothing at all when the
          current principal has nothing expiring. */}
      <OwnershipExpiryBannerScreen />

      <div className="stew__body">
        <section className="stew__panel" aria-label="Unowned asset backlog">
          <div className="stew__panelhead">
            <div>
              <p className="stew__eyebrow">STEWARDSHIP BACKLOG</p>
              <h2 className="stew__h2">Unowned assets</h2>
            </div>
            {backlogTotal !== null ? (
              <Pill tone="mute">
                {backlogTotal} {candidateOwnerFilter ? "for this owner" : "total"}
              </Pill>
            ) : null}
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

          {/* Advanced filter (design 21 §17): the free-text half of the
              filtering pattern `NegativeKnowledgeScreen` already establishes
              -- a categorical select (there: Suppression; here: Status) next
              to a free-text field (there: Assertion type; here: Candidate
              owner). Sent to the server with Status, so it narrows the whole
              backlog; applied on submit because the match is exact. */}
          <form
            className="stew__backlogfilters"
            onSubmit={(e) => {
              e.preventDefault();
              setCandidateOwnerFilter(candidateOwnerDraft.trim());
            }}
          >
            <Field label="Candidate owner">
              <input
                type="text"
                list="stew-candidate-owners"
                value={candidateOwnerDraft}
                placeholder="e.g. risk-data-stewards@tenant.example"
                onChange={(e) => setCandidateOwnerDraft(e.target.value)}
              />
            </Field>
            <datalist id="stew-candidate-owners">
              {knownCandidateOwners.map((owner) => (
                <option key={owner} value={owner} />
              ))}
            </datalist>
            <Button type="submit" disabled={candidateOwnerDraft.trim() === candidateOwnerFilter}>
              Apply filter
            </Button>
            {candidateOwnerFilter || candidateOwnerDraft ? (
              <Button
                onClick={() => {
                  setCandidateOwnerDraft("");
                  setCandidateOwnerFilter("");
                }}
              >
                Clear filter
              </Button>
            ) : null}
          </form>

          {routeError ? <p className="stew__err" role="alert">{routeError}</p> : null}
          {routeResult ? <RouteResultSummary result={routeResult} /> : null}

          {backlogError ? (
            <ErrorState title="The unowned backlog could not be loaded" detail={backlogError} onRetry={() => void loadBacklog()} />
          ) : backlogLoading ? (
            <p className="stew__note">Loading…</p>
          ) : backlog.length === 0 ? (
            <Empty
              title={
                candidateOwnerFilter
                  ? "No unowned assets for this candidate owner"
                  : "No unowned assets in this status"
              }
              hint={
                candidateOwnerFilter
                  ? "The candidate owner is matched exactly as stored, capital letters included. Clear the filter to see the whole backlog."
                  : dsLabel
                    ? undefined
                    : "Ownership coverage is clear for the current scope."
              }
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
