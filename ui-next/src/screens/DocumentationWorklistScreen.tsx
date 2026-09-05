import { useCallback, useEffect, useState } from "react";
import type { DocumentationWorklistEntryRead } from "../lib/ui-types";
import { ApiError, fetchDocumentationWorklist } from "../lib/api";
import { useOrgId } from "../lib/org";
import { useUrlState } from "../lib/useUrlState";
import { Empty, ErrorState, Field, Pill } from "../components/primitives";
import "./DocumentationWorklistScreen.css";

/* ---------------------------------------------------------------------------
   Documentation worklist — AT-5 / SW-1.

   "A steward facing 400,000 undocumented objects does not need a better
   editor, they need to know which forty matter." (stewardship_worklist.py's
   own module docstring.) The backend has ranked this for a while --
   `usage x impact x deficit`, real query volume times downstream blast
   radius times a five-field documentation gap -- but nothing ever rendered
   it: `GET .../stewardship/documentation-worklist` had zero callers in
   ui-next before this screen.

   One control worth calling out: `ranking` toggles between SW-1's
   `priority` order (the point of this screen) and the pre-adoption
   `query_volume`-only order, so a steward can see for themselves how much
   the impact/deficit factors actually move the list -- not just take it on
   faith.
--------------------------------------------------------------------------- */

const MISSING_LABEL: Record<string, string> = {
  description: "description",
  owner: "owner",
  certification: "certification",
  glossary_term: "glossary term",
  quality_policy: "quality signal",
};

function relative(iso: string | null): string {
  if (!iso) return "never";
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60_000);
  if (!Number.isFinite(minutes)) return "";
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function FactorBar({ label, value }: { label: string; value: number }) {
  return (
    <span className="wklist__factor" title={`${label}: ${value.toFixed(3)}`}>
      <span className="wklist__factorlabel">{label}</span>
      <span className="wklist__factortrack">
        <span className="wklist__factorfill" style={{ width: `${Math.min(value, 1) * 100}%` }} />
      </span>
      <span className="wklist__factorvalue">{percent(value)}</span>
    </span>
  );
}

function WorklistRow({ item }: { item: DocumentationWorklistEntryRead }) {
  return (
    <li className="wklist__row">
      <div className="wklist__rowhead">
        <span className="wklist__rank">#{item.rank}</span>
        <div className="wklist__name">
          <strong>{item.schema_name}.{item.table_name}</strong>
          <span className="wklist__muted">{item.datasource_name}</span>
        </div>
        <span className="wklist__score" title="score = usage x impact x deficit">
          {item.score.toFixed(3)}
        </span>
      </div>

      <div className="wklist__factors">
        <FactorBar label="usage" value={item.usage} />
        <FactorBar label="impact" value={item.impact} />
        <FactorBar label="deficit" value={item.deficit} />
      </div>

      <div className="wklist__meta">
        <span>{item.query_volume} queries — {item.query_execution_count} governed, {item.consumption_read_count} MCP</span>
        <span>last queried {relative(item.last_queried_at)}</span>
        <span>{item.downstream_count} downstream reference(s)</span>
        {item.description_is_proposed && <Pill tone="warn">description proposed, not approved</Pill>}
      </div>

      {item.missing.length > 0 && (
        <div className="wklist__missing">
          {item.missing.map((field) => (
            <Pill key={field} tone="mute">missing {MISSING_LABEL[field] ?? field}</Pill>
          ))}
        </div>
      )}
    </li>
  );
}

export function DocumentationWorklistScreen() {
  const organizationId = useOrgId();
  const [params, setParams] = useUrlState();
  const ranking = (params.get("ranking") ?? "priority") === "query_volume" ? "query_volume" : "priority";
  const includeZeroVolume = params.get("zero") === "1";

  const [items, setItems] = useState<DocumentationWorklistEntryRead[] | null>(null);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    (signal?: AbortSignal) => {
      setLoading(true);
      setError(null);
      fetchDocumentationWorklist(
        organizationId,
        { ranking, includeZeroVolume, limit: 100 },
        signal,
      )
        .then((page) => {
          setItems(page.items);
          setTotal(page.total);
          setLoading(false);
        })
        .catch((err: unknown) => {
          if (signal?.aborted) return;
          setError(err instanceof ApiError ? err.message : String(err));
          setLoading(false);
        });
    },
    [organizationId, ranking, includeZeroVolume],
  );

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  if (error) {
    return (
      <section className="wklist">
        <ErrorState title="The documentation worklist could not be loaded" detail={error} onRetry={() => load()} />
      </section>
    );
  }

  return (
    <section className="wklist">
      <header className="wklist__head">
        <div>
          <h1>Documentation worklist</h1>
          <p className="wklist__sub">
            Which forty tables actually matter — ranked by real query volume × downstream
            impact × documentation deficit, not alphabetical order and not usage alone.
          </p>
        </div>
        <div className="wklist__controls">
          <Field label="Ranking">
            <select
              value={ranking}
              onChange={(event) => setParams({ ranking: event.target.value === "query_volume" ? "query_volume" : null })}
              aria-label="Ranking"
            >
              <option value="priority">Priority (usage × impact × deficit)</option>
              <option value="query_volume">Query volume only</option>
            </select>
          </Field>
          <label className="wklist__checkbox">
            <input
              type="checkbox"
              checked={includeZeroVolume}
              onChange={(event) => setParams({ zero: event.target.checked ? "1" : null })}
            />
            Include never-queried tables
          </label>
        </div>
      </header>

      {items && (
        <p className="wklist__count">
          {items.length} of {total} candidate table(s) shown.
        </p>
      )}

      {loading && !items ? (
        <p role="status" className="wklist__loading">Loading the worklist…</p>
      ) : items && items.length > 0 ? (
        <ul className="wklist__list">
          {items.map((item) => (
            <WorklistRow key={item.table_id} item={item} />
          ))}
        </ul>
      ) : (
        <Empty
          title="Nothing ranks above zero right now"
          hint="Every candidate table is either never queried or already fully documented."
        />
      )}
    </section>
  );
}
