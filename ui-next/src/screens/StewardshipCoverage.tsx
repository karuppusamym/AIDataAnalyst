import { useEffect, useMemo, useState } from "react";
import type { StewardshipCoverageRead } from "../lib/types";
import {
  COVERAGE_READ_ROLES,
  COVERAGE_SNAPSHOT_ROLES,
  fetchCoverageSnapshots,
  fetchStewardshipCoverage,
  takeCoverageSnapshot,
} from "../lib/api";
import { dimensionCounts, dimensionLabel, orderedDimensionKeys } from "../lib/coverageDimensions";
import { useOrgId } from "../lib/org";
import { readDecision, roleAllows, roleHolds } from "../lib/roles";
import { listOr } from "../lib/sentences";
import { useSession } from "../lib/session";
import { datasourceName, useDatasourcePicker } from "../lib/useDatasourcePicker";
import { useUrlState } from "../lib/useUrlState";
import { Button, ConfirmDialog, Empty, ErrorState, Field, Pill } from "../components/primitives";
import { FormSuccess, useAsyncResource, useSubmitAction } from "../components/screenState";
import { CoverageSnapshots, pct, stamp } from "./CoverageSnapshots";
import "./StewardshipScreen.css";
import "./StewardshipCoverage.css";

/* ---------------------------------------------------------------------------
   Stewardship -> Coverage (R11-AUD08) -- the scorecard.

   THE QUESTION IT ANSWERS: how much of the estate has stewardship actually
   reached? Six measures over the ACTIVE tables in a scope -- documented, owned,
   classified, certified, quality monitored, semantically mapped -- and one
   overall score. The API computed them (`GET .../stewardship/coverage`) and no
   screen asked; the Work queue and Bulk actions next door are where a gap gets
   closed, and this is where a steward finds out how big the gap is.

   EVERY NUMBER IS THE API'S. `covered`, `total` and `percentage` per dimension,
   `table_count` and `overall_score` are shown as returned. Nothing here divides,
   averages or rounds: a percentage this screen worked out for itself would be a
   second definition of coverage, and the API already has the one that counts.
   What a dimension MEANS -- whether an expired certification counts, say -- is
   stated beside it (`lib/coverageDimensions.ts`), because "certified 33%" of an
   undefined thing is not information.

   NOTHING FROM NOTHING. A scope with no active tables comes back from the API as
   all zeros. That is "nothing to score", not "0% covered", and the screen says
   so instead of drawing six empty bars.

   THE FIGURES ARE COMPUTED WHEN THE VIEW OPENS, NOT STORED. A SNAPSHOT is the
   step that stores them, as one row in the scope's history. The dialog says so
   in those words -- and says the other thing that is easy to miss: the server
   computes the figures AGAIN when the snapshot is confirmed
   (`snapshot_stewardship_coverage`), so the stored row can differ from the
   numbers on screen if the estate moved in between. It is a write (a stored row,
   an audit entry, an outbox event), so it is offered only to a session KNOWN to
   hold one of the write roles (`roleHolds`, fail-closed), asked for only after a
   confirmation, and a refusal stays in that confirmation in the server's own
   words. After a success both reads are asked again: what the panel shows is
   always the server's answer, never an edit of it.

   WHO SEES WHAT. The reads are held while `/v1/me` is in flight and never sent
   to a session known to hold none of the read roles (`readDecision`): that
   session is told coverage is not available to its roles rather than shown the
   403 a doomed request would earn.

   SCOPE. The organization, or one datasource (`?ds=`, the estate context the
   workspace already reads). The API's domain and line-of-business scopes are not
   offered. The history is the scope's own -- the organization's does not contain
   a datasource's snapshots -- and the dialog names which scope a snapshot is
   stored under.
--------------------------------------------------------------------------- */

/**
 * The roles `GET /v1/organizations/{organization_id}/datasources` admits.
 *
 * Copied from the surface-control matrix row for
 * `aida.operational_api.list_organization_datasources`
 * (`Docs/50-security/surface-control-matrix.md`): Analyst, DataAdmin,
 * MetadataAdmin, Operations, OrganizationAdmin, PlatformAdmin, ProjectAdmin,
 * Viewer. Coverage admits four roles this list does not (Auditor, DataSteward,
 * Reviewer, SemanticAdmin), so for a session holding only those the scope
 * picker is not asked for and the scorecard offers the organization -- which is
 * what that session can be shown.
 */
const DATASOURCE_LIST_ROLES = [
  "Analyst",
  "DataAdmin",
  "MetadataAdmin",
  "Operations",
  "OrganizationAdmin",
  "PlatformAdmin",
  "ProjectAdmin",
  "Viewer",
];

/** How many stored snapshots the history asks for. The table says when there are more. */
const HISTORY_LIMIT = 50;

const clampPercent = (value: number): number => Math.min(100, Math.max(0, value));

function DimensionTable({ coverage, scopeName }: { coverage: StewardshipCoverageRead; scopeName: string }) {
  const keys = orderedDimensionKeys(Object.keys(coverage.dimensions));
  return (
    <table className="stewcov__dims">
      <caption className="sr-only">
        Coverage of {scopeName} by dimension: the tables covered, of the active tables in scope, and the percentage the
        server computed.
      </caption>
      <thead>
        <tr>
          <th scope="col">Dimension</th>
          <th scope="col" className="stewcov__num">Covered</th>
          <th scope="col">Coverage</th>
        </tr>
      </thead>
      <tbody>
        {keys.map((key) => {
          const dimension = coverage.dimensions[key]!;
          const counts = dimensionCounts(key);
          return (
            <tr key={key}>
              <th scope="row">
                <span className="stewcov__dimname">{dimensionLabel(key)}</span>
                {counts ? <span className="stewcov__def">{counts}</span> : null}
              </th>
              <td className="stewcov__num">
                {dimension.covered} of {dimension.total}
                <span className="sr-only"> tables</span>
              </td>
              <td>
                <span className="stewcov__barcell">
                  {/* Decorative: the percentage beside it is the value, in text. */}
                  <span className="stewcov__bar" aria-hidden="true">
                    <i style={{ width: `${clampPercent(dimension.percentage)}%` }} />
                  </span>
                  <span className="stewcov__pct">{pct(dimension.percentage)}</span>
                </span>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export function StewardshipCoverage() {
  const ORG = useOrgId();
  const session = useSession();
  const roles = session.me?.roles;
  const read = readDecision(session, COVERAGE_READ_ROLES);
  // A write control is offered only to a session KNOWN to hold a write role; a moment of not
  // showing it costs nothing, and showing it on a guess invites a click the server refuses.
  const mayTake = roleHolds(roles, COVERAGE_SNAPSHOT_ROLES);
  // "Only X can..." is a statement about THIS session, so it waits for identity.
  const identityKnown = roles !== undefined;

  const [params, setParams] = useUrlState();
  const ds = params.get("ds") ?? "";
  const scope = useMemo(() => ({ datasourceId: ds || null }), [ds]);

  /* The tenant's sources, not the ones the active scope reaches: coverage is an organization-level
     read, and a steward scoping it should see every source it can be scoped to. Not asked for by a
     session the list would refuse. */
  const sources = useDatasourcePicker(ORG, {
    reach: "organization",
    selectedId: ds || null,
    enabled: read === "ask" && roleAllows(roles, DATASOURCE_LIST_ROLES),
  });
  const scopeName = ds ? (datasourceName(sources.datasources, ds) ?? "the selected datasource") : "the whole organization";

  const coverage = useAsyncResource<StewardshipCoverageRead>(
    (signal) => fetchStewardshipCoverage(ORG, scope, signal),
    [ORG, ds],
    { enabled: read === "ask" },
  );
  const history = useAsyncResource(
    (signal) => fetchCoverageSnapshots(ORG, scope, { limit: HISTORY_LIMIT }, signal),
    [ORG, ds],
    { enabled: read === "ask" },
  );
  const reloadCoverage = coverage.reload;
  const reloadHistory = history.reload;

  const [confirming, setConfirming] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const take = useSubmitAction<StewardshipCoverageRead>();
  const resetTake = take.reset;

  // A confirmation or a notice belongs to the scope it was made for.
  useEffect(() => {
    setConfirming(false);
    setNotice(null);
    resetTake();
  }, [ds, resetTake]);

  const confirm = async () => {
    const stored = await take.run(() => takeCoverageSnapshot(ORG, scope));
    if (stored === null) return; // the refusal is in `take.error`, shown in the dialog
    setConfirming(false);
    setNotice(
      `Snapshot stored for ${scopeName}: ${stored.table_count} active ${stored.table_count === 1 ? "table" : "tables"}, overall ${pct(stored.overall_score)}.`,
    );
    reloadCoverage();
    reloadHistory();
  };

  const data = coverage.data;
  const dsListNote = read === "ask" && sources.error ? `The datasource list could not be loaded: ${sources.error}` : null;

  return (
    <div className="stew stewcov">
      <header className="stew__head">
        <div>
          <h1 className="stew__h1">Coverage</h1>
          <p className="stew__lede">
            How much of the estate stewardship has reached: six measures over the active tables in a scope, worked out
            from the catalog when this view opens. A snapshot stores those figures so they can be compared over time.
          </p>
        </div>
      </header>

      <div className="stew__body">
        {read === "skip" ? (
          <section className="stew__panel" aria-label="Coverage">
            <p className="stew__note">
              Not applicable to your roles: only sessions holding {listOr(COVERAGE_READ_ROLES)} can read stewardship
              coverage.
            </p>
          </section>
        ) : read === "wait" ? (
          <section className="stew__panel" aria-label="Coverage">
            <p className="stew__note" role="status">Loading coverage…</p>
          </section>
        ) : (
          <>
            <section className="stew__panel" aria-label="Current coverage">
              <div className="stew__panelhead">
                <div>
                  <p className="stew__eyebrow">COVERAGE NOW</p>
                  <h2 className="stew__h2">Stewardship coverage</h2>
                </div>
                {data ? <Pill tone="mute">{data.table_count} active {data.table_count === 1 ? "table" : "tables"}</Pill> : null}
              </div>

              <div className="stewcov__controls">
                <Field label="Scope">
                  <select value={ds} onChange={(event) => setParams({ ds: event.target.value || null })}>
                    <option value="">Whole organization</option>
                    {/* A scope named in the URL that the list has not (yet, or ever) returned: kept as an
                        option so the select never shows a scope other than the one being read. */}
                    {ds && !sources.datasources.some((source) => source.id === ds) ? (
                      <option value={ds}>{scopeName === "the selected datasource" ? "Selected datasource" : scopeName}</option>
                    ) : null}
                    {sources.datasources.map((source) => (
                      <option key={source.id} value={source.id}>{source.name}</option>
                    ))}
                  </select>
                </Field>
                {mayTake ? (
                  <Button
                    variant="primary"
                    disabled={coverage.loading}
                    onClick={() => {
                      take.reset();
                      setNotice(null);
                      setConfirming(true);
                    }}
                  >
                    Take a snapshot
                  </Button>
                ) : null}
              </div>
              {dsListNote ? <p className="stew__note" role="status">{dsListNote}</p> : null}
              {!mayTake && identityKnown ? (
                <p className="stew__note">
                  Only {listOr(COVERAGE_SNAPSHOT_ROLES)} can take a snapshot; everyone who can read coverage sees the
                  history below.
                </p>
              ) : null}
              {notice ? <FormSuccess>{notice}</FormSuccess> : null}

              {coverage.error ? (
                <ErrorState title="Coverage could not be loaded" detail={coverage.error} onRetry={reloadCoverage} />
              ) : coverage.loading || !data ? (
                <p className="stew__note" role="status">Loading coverage…</p>
              ) : data.table_count === 0 ? (
                <Empty
                  title={`No active tables in ${scopeName}`}
                  hint="There is nothing to score yet, so no percentage is shown. Tables count once a source has been discovered and they are active."
                />
              ) : (
                <>
                  <p className="stewcov__overall">
                    <span className="stewcov__score">{pct(data.overall_score)}</span>
                    <span>
                      overall for {scopeName} — the average of the dimension percentages below, across{" "}
                      {data.table_count} active {data.table_count === 1 ? "table" : "tables"}
                    </span>
                  </p>
                  <DimensionTable coverage={data} scopeName={scopeName} />
                  <p className="stew__note">
                    Computed {stamp(data.computed_at)}. These figures are worked out when this view opens and are not
                    stored until someone takes a snapshot.
                  </p>
                </>
              )}
            </section>

            <section className="stew__panel" aria-label="Snapshot history">
              <div className="stew__panelhead">
                <div>
                  <p className="stew__eyebrow">HISTORY</p>
                  <h2 className="stew__h2">Stored snapshots</h2>
                </div>
                {history.data ? <Pill tone="mute">{history.data.total} stored</Pill> : null}
              </div>
              {history.error ? (
                <ErrorState
                  title="The snapshot history could not be loaded"
                  detail={history.error}
                  onRetry={reloadHistory}
                />
              ) : history.loading || !history.data ? (
                <p className="stew__note" role="status">Loading snapshot history…</p>
              ) : history.data.items.length === 0 ? (
                <Empty
                  title={`No snapshots have been stored for ${scopeName}`}
                  hint={
                    mayTake
                      ? "Take a snapshot to start a history you can compare later figures against."
                      : "Someone who can take a snapshot has not yet stored one for this scope."
                  }
                />
              ) : (
                <CoverageSnapshots snapshots={history.data.items} total={history.data.total} />
              )}
            </section>
          </>
        )}
      </div>

      {/* "Destructive" here only means a click outside does not dismiss it. */}
      {confirming ? (
        <ConfirmDialog
          title="Take a coverage snapshot?"
          description={`This stores a snapshot in the coverage history for ${scopeName}: the number of active tables, the six percentages and the overall score, with your name and the time. It is a stored record, recorded in the audit ledger, and this screen has no way to remove it. The server works the figures out again when you confirm, so the stored row can differ from the figures on screen if the estate has changed since. No table, owner or certification changes.`}
          confirmLabel="Take snapshot"
          destructive
          busy={take.submitting}
          error={take.error}
          onConfirm={() => void confirm()}
          onCancel={() => {
            take.reset();
            setConfirming(false);
          }}
        />
      ) : null}
    </div>
  );
}
