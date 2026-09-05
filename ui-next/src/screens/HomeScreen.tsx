import { useEffect, useMemo, useState } from "react";
import { Button, Pill } from "../components/primitives";
import { OnboardingWizard } from "../components/OnboardingWizard";
import { fetchCatalogRows, fetchOrgDatasources, fetchReviewQueue } from "../lib/api";
import { useOrgId } from "../lib/org";
import type { DataSourceRead, ReviewQueueRead } from "../lib/types";
import type { CatalogRowRead, Persona } from "../lib/ui-types";
import type { Tone } from "../components/primitives";
import "./HomeScreen.css";

const nf = new Intl.NumberFormat("en-US");

interface OverviewData {
  assets: CatalogRowRead[];
  assetTotal: number | null;
  sources: DataSourceRead[];
  reviews: ReviewQueueRead | null;
}

const EMPTY_DATA: OverviewData = { assets: [], assetTotal: null, sources: [], reviews: null };

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
  const [data, setData] = useState<OverviewData>(EMPTY_DATA);
  const [loading, setLoading] = useState(true);
  const [partialError, setPartialError] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setPartialError(false);

    Promise.allSettled([
      fetchCatalogRows({ organizationId, limit: 12 }, controller.signal),
      fetchOrgDatasources(organizationId, controller.signal),
      fetchReviewQueue({ status: null, limit: 1000 }, controller.signal),
    ]).then(([catalog, sources, reviews]) => {
      if (controller.signal.aborted) return;
      setData({
        assets: catalog.status === "fulfilled" ? catalog.value.items : [],
        assetTotal: catalog.status === "fulfilled" ? (catalog.value.total ?? null) : null,
        sources: sources.status === "fulfilled" ? sources.value.items : [],
        reviews: reviews.status === "fulfilled" ? reviews.value : null,
      });
      setPartialError([catalog, sources, reviews].some((result) => result.status === "rejected"));
      setLoading(false);
    });

    return () => controller.abort();
  }, [organizationId]);

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
      pendingReviews: data.reviews?.by_status.PENDING ?? 0,
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
        <button className="homekpi" onClick={() => onNavigate("governance")}>
          <span className="homekpi__icon homekpi__icon--amber" aria-hidden="true">!</span>
          <span><b>{loading ? "—" : nf.format(summary.pendingReviews)}</b><small>Decisions waiting</small></span>
          <i aria-hidden="true">→</i>
        </button>
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
              <button onClick={() => onNavigate("governance")}><span className="homeattention__signal homeattention__signal--amber">{summary.pendingReviews}</span><span><b>Review decisions</b><small>Governed changes awaiting judgment</small></span><i>→</i></button>
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
