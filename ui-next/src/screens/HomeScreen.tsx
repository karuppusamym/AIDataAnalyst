import { useEffect, useMemo, useState } from "react";
import { Button, Pill } from "../components/primitives";
import { OnboardingWizard } from "../components/OnboardingWizard";
/* T15: setup readiness derived from the server, not from checkboxes. It leads
   the page for an estate that is not finished being set up, because the review
   is explicit that for an empty installation the prerequisites must lead the
   checklist -- the previous ordering put Administration after Sources and
   Operations, which is the order in which they cannot be done. */
import { FirstSourceSetup } from "../components/FirstSourceSetup";
import { fetchCatalogRows, fetchReviewQueue, get, listOrgDatasources, USE_FIXTURES } from "../lib/api";
import { useOrgId } from "../lib/org";
import { readDecision } from "../lib/roles";
import { useSession } from "../lib/session";
import type { DataSourceRead, ReviewQueueSummaryRead } from "../lib/types";
import type { CatalogRowRead, Persona } from "../lib/ui-types";
import type { Tone } from "../components/primitives";
import "./HomeScreen.css";

const nf = new Intl.NumberFormat("en-US");

interface OverviewData {
  assets: CatalogRowRead[];
  assetTotal: number | null;
  sources: DataSourceRead[];
}

const EMPTY_DATA: OverviewData = { assets: [], assetTotal: null, sources: [] };

/**
 * The roles `GET /v1/governance/reviews/queue/summary` admits.
 *
 * Copied from the surface-control matrix row for
 * `aida.review_queue_api.get_review_queue_summary`
 * (`Docs/50-security/surface-control-matrix.md`): DataSteward, PlatformAdmin,
 * Reviewer, SemanticAdmin. Any other session gets a 403 on every load of this
 * page, so it is not asked (R11-AUD01: the demo rehearsal found it for an
 * AgentDeveloper).
 */
const REVIEW_QUEUE_SUMMARY_ROLES = ["DataSteward", "PlatformAdmin", "Reviewer", "SemanticAdmin"];

const REVIEW_QUEUE_NOT_APPLICABLE =
  "Only sessions holding DataSteward, PlatformAdmin, Reviewer or SemanticAdmin read the review queue, " +
  "so this signal does not apply to your roles.";

/**
 * The review-queue signal, as four different things that used to be one `0`.
 *
 * A read that was refused, a read that failed, a read still in flight and a
 * session that was never entitled to ask all rendered as "0 decisions
 * waiting" -- the last two of those are not zero, and the first was a page
 * telling a non-reviewer there was nothing to review because it had been
 * refused the count.
 */
type ReviewSignal =
  | { kind: "loading" }
  | { kind: "count"; value: number }
  /** The read was attempted and did not come back. `partialError` says so. */
  | { kind: "unavailable" }
  /** This session's roles are not admitted; the read was never issued. */
  | { kind: "not-applicable" };

/**
 * How many reviews are pending (review 2026-09-05, F16).
 *
 * THE DEFECT this removes: Overview asked `GET /v1/governance/reviews/queue`
 * for up to 1,000 proposals -- each one composing a structured diff from the
 * database, with its evidence and its semantic/glossary snapshots -- and then
 * used nothing from the response except one integer out of `by_status`. The
 * cost of rendering a number on the landing page grew with the size of the
 * review backlog.
 *
 * `/queue/summary` is one grouped COUNT(*): it composes nothing and its
 * response size does not depend on queue depth. The fixtures build keeps
 * reading the fixture queue because there is no server to aggregate for it and
 * the array is already in memory -- the N+1 this replaces was a database
 * shape, not a client one.
 */
async function fetchPendingReviewCount(signal: AbortSignal): Promise<number> {
  if (USE_FIXTURES) {
    const queue = await fetchReviewQueue({ status: "PENDING", limit: 1000 }, signal);
    return queue.by_status["PENDING"] ?? 0;
  }
  const summary = await get<ReviewQueueSummaryRead>(
    "/v1/governance/reviews/queue/summary?status=PENDING",
    signal,
  );
  return summary.total;
}

const certTone = (status: CatalogRowRead["certification"]): Tone =>
  status === "CERTIFIED" ? "ok" : status === "EXPIRED" ? "warn" : status === "REVOKED" ? "bad" : "mute";

function relativeDate(value: string): string {
  const days = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 86_400_000));
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  return `${days}d ago`;
}

export function HomeScreen({
  persona,
  onNavigate,
}: {
  persona: Persona | null;
  /** The shell's own `navigate`. `params` ride alongside the hash route so a
   *  row opened from here lands on the target screen already focused -- the
   *  same `useUrlState` convention every migrated screen reads. */
  onNavigate: (navId: string, params?: Record<string, string>) => void;
}) {
  const organizationId = useOrgId();
  // The count is held while `/v1/me` is in flight (`readDecision`, `lib/roles.ts`) and
  // then asked for only by a session in the matrix row: a session outside it is never
  // asked, and never takes the refusal the request would have earned.
  const queueRead = readDecision(useSession(), REVIEW_QUEUE_SUMMARY_ROLES);

  const [data, setData] = useState<OverviewData>(EMPTY_DATA);
  const [loading, setLoading] = useState(true);
  const [overviewFailed, setOverviewFailed] = useState(false);
  // Seeded from the roles, so a session that is already known not to be
  // admitted never paints a clickable "—" tile for the frame before the effect
  // below turns it into "not applicable".
  const [reviews, setReviews] = useState<ReviewSignal>(
    queueRead === "skip" ? { kind: "not-applicable" } : { kind: "loading" },
  );

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setOverviewFailed(false);

    Promise.allSettled([
      fetchCatalogRows({ organizationId, limit: 12 }, controller.signal),
      listOrgDatasources(organizationId, controller.signal),
    ]).then(([catalog, sources]) => {
      if (controller.signal.aborted) return;
      setData({
        assets: catalog.status === "fulfilled" ? catalog.value.items : [],
        assetTotal: catalog.status === "fulfilled" ? (catalog.value.total ?? null) : null,
        sources: sources.status === "fulfilled" ? sources.value.items : [],
      });
      setOverviewFailed([catalog, sources].some((result) => result.status === "rejected"));
      setLoading(false);
    });

    return () => controller.abort();
  }, [organizationId]);

  /* Its own effect, not a third arm of the one above: the answer to "may this
     session read the queue" arrives when `/v1/me` does, which is usually after
     the catalog request has gone out. Sharing an effect would re-fetch the
     catalog and the source list -- and flash the whole page back to loading --
     just because identity resolved. */
  useEffect(() => {
    if (queueRead === "skip") {
      setReviews({ kind: "not-applicable" });
      return;
    }
    if (queueRead === "wait") {
      setReviews({ kind: "loading" });
      return;
    }
    const controller = new AbortController();
    setReviews({ kind: "loading" });
    fetchPendingReviewCount(controller.signal).then(
      (value) => {
        if (!controller.signal.aborted) setReviews({ kind: "count", value });
      },
      () => {
        if (!controller.signal.aborted) setReviews({ kind: "unavailable" });
      },
    );
    return () => controller.abort();
  }, [organizationId, queueRead]);

  const partialError = overviewFailed || reviews.kind === "unavailable";

  const summary = useMemo(() => {
    const sampled = data.assets.length;
    const certified = data.assets.filter((asset) => asset.certification === "CERTIFIED").length;
    const documented = data.assets.filter((asset) => asset.description && !asset.description_is_proposed).length;
    const needsOwner = data.assets.filter((asset) => !asset.owner).length;
    const qualityAlerts = data.assets.filter((asset) => asset.quality === "INCIDENT_OPEN" || asset.quality === "STALE").length;
    return {
      sampled,
      certified,
      documented,
      needsOwner,
      qualityAlerts,
      activeSources: data.sources.filter((source) => source.status === "ACTIVE").length,
    };
  }, [data]);

  const trustedPercent = summary.sampled ? Math.round((summary.certified / summary.sampled) * 100) : null;
  const documentedPercent = summary.sampled ? Math.round((summary.documented / summary.sampled) * 100) : null;

  return (
    <div className="home">
      <section className="homehero">
        <div className="homehero__copy">
          <div className="homehero__eyebrow">{persona ? `${persona} workspace` : "Data intelligence workspace"}</div>
          <h1>Find the right data.<br />Know why you can trust it.</h1>
          <p>Discover governed assets, understand their meaning and lineage, and make decisions with the evidence already attached.</p>
          <div className="homehero__actions">
            <Button variant="primary" onClick={() => onNavigate("catalog")}>Explore catalog</Button>
            <Button onClick={() => onNavigate("analyst")}>Ask Atlas</Button>
            <Button onClick={() => onNavigate("developer")}>Connect an agent</Button>
          </div>
        </div>
        <div className="homehero__visual" aria-hidden="true">
          <span className="homehero__orbit homehero__orbit--one" />
          <span className="homehero__orbit homehero__orbit--two" />
          <span className="homehero__node homehero__node--center">A</span>
          <span className="homehero__node homehero__node--top">DQ</span>
          <span className="homehero__node homehero__node--left">SQL</span>
          <span className="homehero__node homehero__node--right">AI</span>
        </div>
      </section>

      {partialError ? <div className="home__notice" role="status">Some workspace signals are temporarily unavailable. Available data is shown below.</div> : null}

      <FirstSourceSetup onNavigate={onNavigate} />

      <section className="homekpis" aria-label="Workspace summary">
        <button className="homekpi" onClick={() => onNavigate("catalog")}>
          <span className="homekpi__icon homekpi__icon--violet" aria-hidden="true">▦</span>
          <span><b>{loading ? "—" : data.assetTotal === null ? "—" : nf.format(data.assetTotal)}</b><small>Catalog assets</small></span>
          <i aria-hidden="true">→</i>
        </button>
        <button className="homekpi" onClick={() => onNavigate("catalog")}>
          <span className="homekpi__icon homekpi__icon--green" aria-hidden="true">✓</span>
          <span><b>{loading || trustedPercent === null ? "—" : `${trustedPercent}%`}</b><small>Certified in latest sample</small></span>
          <i aria-hidden="true">→</i>
        </button>
        <button className="homekpi" onClick={() => onNavigate("sources")}>
          <span className="homekpi__icon homekpi__icon--blue" aria-hidden="true">▱</span>
          <span><b>{loading ? "—" : nf.format(summary.activeSources)}</b><small>Active data sources</small></span>
          <i aria-hidden="true">→</i>
        </button>
        {reviews.kind === "not-applicable" ? (
          /* Not a button: the screen it would open is the review queue, which is
             the surface this session was just found not to be admitted to. */
          <div className="homekpi homekpi--na" title={REVIEW_QUEUE_NOT_APPLICABLE}>
            <span className="homekpi__icon homekpi__icon--amber" aria-hidden="true">!</span>
            <span><b>Not applicable</b><small>Decisions waiting · reviewer roles only</small></span>
          </div>
        ) : (
          <button className="homekpi" onClick={() => onNavigate("governance")}>
            <span className="homekpi__icon homekpi__icon--amber" aria-hidden="true">!</span>
            <span><b>{reviews.kind === "count" ? nf.format(reviews.value) : "—"}</b><small>Decisions waiting</small></span>
            <i aria-hidden="true">→</i>
          </button>
        )}
      </section>

      <div className="homegrid">
        <section className="homepanel homepanel--assets">
          <header className="homepanel__head">
            <div><span className="homepanel__eyebrow">Discovery</span><h2>Recently updated assets</h2></div>
            <button className="homepanel__link" onClick={() => onNavigate("catalog")}>View catalog <span aria-hidden="true">→</span></button>
          </header>
          <div className="hometable" role="table" aria-label="Recently updated assets">
            <div className="hometable__row hometable__row--head" role="row">
              <span role="columnheader">Asset</span><span role="columnheader">Source</span><span role="columnheader">Owner</span><span role="columnheader">Trust</span><span role="columnheader">Updated</span>
            </div>
            {loading ? <div className="hometable__empty">Loading recent assets…</div> : data.assets.length === 0 ? <div className="hometable__empty">No assets are available yet.</div> : data.assets.slice(0, 6).map((asset) => (
              <button key={asset.id} className="hometable__row" role="row" onClick={() => onNavigate("catalog", { asset: asset.id })}>
                <span className="hometable__asset" role="cell"><b>{asset.name}</b><small>{asset.schema_name} · {asset.object_type.toLowerCase().replace(/_/g, " ")}</small></span>
                <span className="hometable__source" role="cell">{asset.datasource_name}</span>
                <span className={asset.owner ? "" : "hometable__muted"} role="cell">{asset.owner ?? "Needs owner"}</span>
                <span role="cell"><Pill tone={certTone(asset.certification)}>{asset.certification.toLowerCase()}</Pill></span>
                <time role="cell" dateTime={asset.updated_at}>{relativeDate(asset.updated_at)}</time>
              </button>
            ))}
          </div>
        </section>

        <aside className="homerail">
          <section className="homepanel homeattention">
            <header className="homepanel__head"><div><span className="homepanel__eyebrow">Focus</span><h2>Needs attention</h2></div></header>
            <div className="homeattention__list">
              {/* Omitted, not zeroed, for a session that may not read the queue: a
                  "0" here told a non-reviewer there was nothing to review. */}
              {reviews.kind === "not-applicable" ? null : (
                <button onClick={() => onNavigate("governance")}><span className="homeattention__signal homeattention__signal--amber">{reviews.kind === "count" ? reviews.value : "—"}</span><span><b>Review decisions</b><small>Governed changes awaiting judgment</small></span><i>→</i></button>
              )}
              <button onClick={() => onNavigate("catalog")}><span className="homeattention__signal homeattention__signal--violet">{summary.needsOwner}</span><span><b>Ownership gaps</b><small>Unowned assets in the latest sample</small></span><i>→</i></button>
              <button onClick={() => onNavigate("quality")}><span className="homeattention__signal homeattention__signal--red">{summary.qualityAlerts}</span><span><b>Quality signals</b><small>Open or stale in the latest sample</small></span><i>→</i></button>
            </div>
            <div className="homeattention__coverage"><span><b>{documentedPercent ?? "—"}{documentedPercent === null ? "" : "%"}</b> documented</span><span className="homeattention__track"><i style={{ width: `${documentedPercent ?? 0}%` }} /></span></div>
          </section>

          <OnboardingWizard compact persona={persona} onNavigate={onNavigate} />
        </aside>
      </div>
    </div>
  );
}
