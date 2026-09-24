import type { ModelRouteOutcomeRead } from "../lib/types";
import { fetchModelRouteOutcomes } from "../lib/api";
import { Empty, ErrorState } from "../components/primitives";
import { useAsyncResource } from "../components/screenState";

/* ---------------------------------------------------------------------------
   R11-MP11: how each model route has actually done.

   Counted server-side (`GET /v1/organizations/{org}/model-route-outcomes`,
   `aida/model_route_outcomes.py`) from what each Ask run recorded: which route
   answered, whether the chain fell back or skipped a cooling-down route,
   whether the statement needed a repair and came out valid, how a second
   candidate agreed, and what the provider stated it charged. Read-only.

   Every figure is a count over a window of real runs, not a benchmark score,
   and the panel says so -- including when the server's bound cut the window
   short, so a partial count is never read as the whole.
--------------------------------------------------------------------------- */

const WINDOW_DAYS = 7;

function share(part: number, whole: number): string {
  return whole > 0 ? `${Math.round((part / whole) * 100)}%` : "—";
}

function OutcomeRow({ outcome }: { outcome: ModelRouteOutcomeRead }) {
  const agreed = outcome.candidates_identical + outcome.candidates_same_sources;
  return (
    <tr>
      <td>
        <span className="aig__primary">{outcome.route_key}</span>
      </td>
      <td className="tnum">{outcome.runs}</td>
      <td className="tnum">
        {share(outcome.completed, outcome.runs)}
        <span className="aig__secondary">
          {outcome.rejected} refused{outcome.failed ? `, ${outcome.failed} failed` : ""}
        </span>
      </td>
      <td className="tnum">
        {outcome.fallback_runs}
        {outcome.circuit_skips ? (
          <span className="aig__secondary">{outcome.circuit_skips} skipped while cooling down</span>
        ) : null}
      </td>
      <td className="tnum">
        {outcome.repairs_attempted ? (
          <>
            {outcome.repairs_valid}/{outcome.repairs_attempted}
            <span className="aig__secondary">repaired to valid</span>
          </>
        ) : (
          "—"
        )}
      </td>
      <td className="tnum">
        {outcome.candidates_compared ? (
          <>
            {share(agreed, outcome.candidates_compared)}
            <span className="aig__secondary">
              of {outcome.candidates_compared} agreed ({outcome.candidates_identical} identical)
            </span>
          </>
        ) : (
          "—"
        )}
      </td>
      <td className="tnum">
        {outcome.stated_cost_usd === null ? "—" : `$${outcome.stated_cost_usd.toFixed(4)}`}
      </td>
    </tr>
  );
}

export function RouteOutcomesPanel({ organizationId }: { organizationId: string }) {
  const outcomes = useAsyncResource(
    (signal) => fetchModelRouteOutcomes(organizationId, WINDOW_DAYS, signal),
    [organizationId],
  );
  const data = outcomes.data;
  return (
    <article className="aig__panel" aria-labelledby="aig-route-outcomes">
      <div className="aig__panelhead aig__panelhead--padded">
        <div>
          <p className="aig__eyebrow">ROUTE OUTCOMES</p>
          <h2 className="aig__h2" id="aig-route-outcomes">
            How each route has done, last {WINDOW_DAYS} days
          </h2>
          <p className="aig__lede">
            Counted from the Ask runs themselves, not a benchmark. Cost is shown only where the
            provider stated one.
          </p>
        </div>
      </div>
      {outcomes.error ? (
        <ErrorState
          title="Route outcomes could not be loaded"
          detail={outcomes.error}
          onRetry={outcomes.reload}
        />
      ) : outcomes.loading || !data ? (
        <div className="aig__skeleton" role="status">
          Loading route outcomes…
        </div>
      ) : data.routes.length === 0 ? (
        <Empty
          title="No Ask run reached a model in this window"
          hint="Runs answered by a governed tool, or refused before generation, belong to no route."
        />
      ) : (
        <>
          <div className="aig__scroll">
            <table className="aig__table">
              <thead>
                <tr>
                  <th>Route</th>
                  <th>Runs</th>
                  <th>Completed</th>
                  <th>Fell back</th>
                  <th>Repairs</th>
                  <th>Second candidate</th>
                  <th>Stated cost</th>
                </tr>
              </thead>
              <tbody>
                {data.routes.map((outcome) => (
                  <OutcomeRow key={outcome.route_key} outcome={outcome} />
                ))}
              </tbody>
            </table>
          </div>
          <p className="aig__hint">
            {data.runs_considered} run{data.runs_considered === 1 ? "" : "s"} counted
            {data.truncated ? ": the newest only, because the window held more than the server reads at once." : "."}
          </p>
        </>
      )}
    </article>
  );
}
