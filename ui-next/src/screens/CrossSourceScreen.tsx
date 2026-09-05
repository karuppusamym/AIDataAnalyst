import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, fetchOrgDatasources } from "../lib/api";
import {
  decideCrossSourceResolutionCandidate,
  decideRelationshipCandidate,
  discoverCrossSourceObjectResolutions,
  discoverCrossSourceRelationships,
  domainsWithDatasources,
  fetchCrossSourceRelationshipCandidates,
  fetchCrossSourceResolutionCandidates,
  fetchOrgDataDomains,
  type CrossSourceResolutionCandidateRead,
  type RelationshipCandidateRead,
} from "../lib/_cross_source_api";
import { CrossBoundaryGrants } from "../components/CrossBoundaryGrants";
import type { DataDomainRead, DataSourceRead } from "../lib/types";
import { useUrlState } from "../lib/useUrlState";
import { useOrgId } from "../lib/org";
import { Button, Empty, ErrorState, Field, Pill } from "../components/primitives";
import type { Tone } from "../components/primitives";
import "./CrossSourceScreen.css";

/* ---------------------------------------------------------------------------
   Cross-source — nav id `cross-source`.

   Relationship inference and object resolution are the two questions that
   cannot be asked one datasource at a time:

     "does this column point at a table in another system?"   relationships
     "are these two tables the same real-world object?"       object resolution

   Both are addressed by *data domain*, because the domain is this platform's
   governance boundary (ADR-0017): pairing two datasources inside one domain is
   free, and pairing across into a second domain needs an ACTIVE
   `cross_boundary_grant`. Every other screen in this app is scoped to a single
   datasource, so neither question had a surface here at all — the endpoints
   have been on the server the whole time with nothing calling them.

   Three rules this screen encodes:

   1. **Discovery proposes; it never decides.** Both discover actions write
      PENDING candidates. Approving one is a separate, maker-checker-guarded
      decision, and the server refuses a principal reviewing a candidate they
      created.
   2. **A refused cross-boundary scan is not an error to swallow.** Asking to
      pair against a domain you have no grant for answers 403 by design. That
      is turned into the grant request, not into a red banner that leaves the
      steward guessing what to do next.
   3. **Confidence is shown, never used as a threshold.** Nothing here
      auto-approves above a score. The number orders the queue so the strongest
      evidence is read first; a human still decides every row.
   4. **Both kinds are reviewed here, not just proposed here.** Cross-source
      relationship candidates were originally discovered on this screen and
      reviewed on the per-datasource Relationships screen -- the one scope that
      cannot express "this column points into another system", so the rows a
      steward most needed were the ones its filter hid. Both queues live here.
      Same-source candidates deliberately do not: those already have a home,
      and duplicating them would make this a second, differently-filtered copy
      of that screen.
--------------------------------------------------------------------------- */

const STATUS_TONE: Record<string, Tone> = {
  PENDING: "info",
  APPROVED: "ok",
  REJECTED: "bad",
};

function confidenceTone(confidence: number): Tone {
  if (confidence >= 0.8) return "ok";
  if (confidence >= 0.6) return "warn";
  return "mute";
}

function sourceName(datasources: readonly DataSourceRead[], id: string): string {
  return datasources.find((d) => d.id === id)?.name ?? id;
}

function CandidateRow({
  candidate,
  datasources,
  onDecide,
  busy,
}: {
  candidate: CrossSourceResolutionCandidateRead;
  datasources: readonly DataSourceRead[];
  onDecide: (id: string, decision: "APPROVE" | "REJECT") => void;
  busy: boolean;
}) {
  const evidence = Object.entries(candidate.evidence ?? {});
  return (
    <li className="xs__row">
      <div className="xs__rowhead">
        <span className="xs__pair">
          <span className="xs__side">
            {sourceName(datasources, candidate.source_datasource_id)}
            <span className="xs__table">{candidate.source_table_id}</span>
          </span>
          <span className="xs__equals" aria-label="may be the same object as">
            ≡
          </span>
          <span className="xs__side">
            {sourceName(datasources, candidate.target_datasource_id)}
            <span className="xs__table">{candidate.target_table_id}</span>
          </span>
        </span>
        <span className="xs__spacer" />
        <Pill tone={confidenceTone(candidate.confidence)}>
          {candidate.confidence.toFixed(2)}
        </Pill>
        <Pill tone={STATUS_TONE[candidate.status] ?? "mute"}>
          {candidate.status.toLowerCase()}
        </Pill>
      </div>

      <div className="xs__rule">{candidate.detection_rule.replace(/_/g, " ").toLowerCase()}</div>

      {evidence.length > 0 ? (
        <div className="xs__evidence">
          {evidence.map(([key, value]) => (
            <span key={key}>
              {key.replace(/_/g, " ")}: {String(value)}
            </span>
          ))}
        </div>
      ) : null}

      {candidate.status === "PENDING" ? (
        <div className="xs__actions">
          <Button
            variant="primary"
            disabled={busy}
            onClick={() => onDecide(candidate.id, "APPROVE")}
          >
            Approve
          </Button>
          <Button disabled={busy} onClick={() => onDecide(candidate.id, "REJECT")}>
            Reject
          </Button>
        </div>
      ) : (
        <div className="xs__decided">
          {candidate.status.toLowerCase()} by {candidate.reviewed_by ?? "—"}
          {candidate.review_reason ? ` · ${candidate.review_reason}` : ""}
        </div>
      )}
    </li>
  );
}

function RelationshipRow({
  candidate,
  datasources,
  onDecide,
  busy,
}: {
  candidate: RelationshipCandidateRead;
  datasources: readonly DataSourceRead[];
  onDecide: (id: string, decision: "APPROVE" | "REJECT") => void;
  busy: boolean;
}) {
  const evidence = Object.entries(candidate.evidence ?? {});
  return (
    <li className="xs__row">
      <div className="xs__rowhead">
        <span className="xs__pair">
          <span className="xs__side">
            <span>{sourceName(datasources, candidate.datasource_id)}</span>
            <span className="xs__table">{candidate.source_column_id}</span>
          </span>
          {/* Directional, unlike an object-resolution pair: a foreign-key-like
              relationship points one way, and rendering it symmetrically would
              misstate which side is the reference. */}
          <span className="xs__equals" aria-label="references">
            →
          </span>
          <span className="xs__side">
            <span>{sourceName(datasources, candidate.target_datasource_id)}</span>
            <span className="xs__table">{candidate.target_column_id}</span>
          </span>
        </span>
        <span className="xs__spacer" />
        <Pill tone={confidenceTone(candidate.confidence)}>
          {candidate.confidence.toFixed(2)}
        </Pill>
        <Pill tone={STATUS_TONE[candidate.status] ?? "mute"}>
          {candidate.status.toLowerCase()}
        </Pill>
      </div>

      <div className="xs__rule">{candidate.detection_rule.replace(/_/g, " ").toLowerCase()}</div>

      {evidence.length > 0 ? (
        <div className="xs__evidence">
          {evidence.map(([key, value]) => (
            <span key={key}>
              {key.replace(/_/g, " ")}: {String(value)}
            </span>
          ))}
        </div>
      ) : null}

      {candidate.status === "PENDING" ? (
        <div className="xs__actions">
          <Button
            variant="primary"
            disabled={busy}
            onClick={() => onDecide(candidate.id, "APPROVE")}
          >
            Approve
          </Button>
          <Button disabled={busy} onClick={() => onDecide(candidate.id, "REJECT")}>
            Reject
          </Button>
        </div>
      ) : (
        <div className="xs__decided">
          {candidate.status.toLowerCase()} by {candidate.reviewed_by ?? "—"}
          {candidate.review_reason ? ` · ${candidate.review_reason}` : ""}
        </div>
      )}
    </li>
  );
}

export function CrossSourceScreen() {
  const ORG = useOrgId();
  const [params, setParams] = useUrlState();
  const dom = params.get("dom");
  const statusFilter = params.get("status") ?? "PENDING";

  const [domains, setDomains] = useState<DataDomainRead[]>([]);
  const [datasources, setDatasources] = useState<DataSourceRead[]>([]);
  const [candidates, setCandidates] = useState<CrossSourceResolutionCandidateRead[] | null>(null);
  const [relationships, setRelationships] = useState<RelationshipCandidateRead[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<"relationships" | "resolutions" | "decision" | null>(null);
  const [targetDomainId, setTargetDomainId] = useState("");
  const [grantTargetDomainId, setGrantTargetDomainId] = useState<string | null>(null);

  useEffect(() => {
    const ac = new AbortController();
    let cancelled = false;
    void (async () => {
      try {
        const [allDomains, sources] = await Promise.all([
          fetchOrgDataDomains(ORG, ac.signal),
          fetchOrgDatasources(ORG, ac.signal),
        ]);
        if (cancelled) return;
        setDatasources(sources.items ?? []);
        setDomains(domainsWithDatasources(allDomains, sources.items ?? []));
      } catch (e) {
        if (!cancelled) setError(e instanceof ApiError ? e.detail : (e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
      ac.abort();
    };
  }, [ORG]);

  const domainDatasourceIds = useMemo(
    () => (dom ? datasources.filter((d) => d.data_domain_id === dom).map((d) => d.id) : []),
    [datasources, dom],
  );

  const loadCandidates = useCallback(
    async (signal?: AbortSignal) => {
      if (domainDatasourceIds.length === 0) {
        setCandidates(dom ? [] : null);
        setRelationships(dom ? [] : null);
        return;
      }
      setError(null);
      const status = statusFilter === "ALL" ? null : statusFilter;
      try {
        // Both queues in one pass: they share a domain, a status filter and a
        // reviewer, and loading them separately would let the screen show one
        // half of the answer.
        const [resolutions, relationshipRows] = await Promise.all([
          fetchCrossSourceResolutionCandidates(domainDatasourceIds, status, signal),
          fetchCrossSourceRelationshipCandidates(domainDatasourceIds, status, signal),
        ]);
        setCandidates(resolutions);
        setRelationships(relationshipRows);
      } catch (e) {
        if ((e as Error)?.name === "AbortError") return;
        setCandidates([]);
        setRelationships([]);
        setError(e instanceof ApiError ? e.detail : (e as Error).message);
      }
    },
    [domainDatasourceIds, statusFilter, dom],
  );

  useEffect(() => {
    const ac = new AbortController();
    setCandidates(null);
    setRelationships(null);
    void loadCandidates(ac.signal);
    return () => ac.abort();
  }, [loadCandidates]);

  const runDiscovery = useCallback(
    async (kind: "relationships" | "resolutions") => {
      if (!dom) return;
      setBusy(kind);
      setNotice(null);
      setError(null);
      try {
        const body = targetDomainId ? { target_data_domain_id: targetDomainId } : {};
        const count =
          kind === "relationships"
            ? await discoverCrossSourceRelationships(dom, body)
            : await discoverCrossSourceObjectResolutions(dom, body);
        const what = kind === "relationships" ? "relationship" : "object resolution";
        setNotice(
          count === 0
            ? `No new ${what} candidates. Nothing this scan could see is unaccounted for.`
            : `${count} ${what} candidate${count === 1 ? "" : "s"} proposed — each still needs a decision.`,
        );
        // Either scan can add rows to the queues below.
        void loadCandidates();
      } catch (e) {
        // A cross-boundary scan without a grant answers 403 by design. Turn
        // that into the request rather than a dead end.
        if (e instanceof ApiError && e.status === 403 && targetDomainId) {
          setGrantTargetDomainId(targetDomainId);
          setError(
            `No active grant lets this domain scan into that one. Request access below, then run the scan again.`,
          );
        } else {
          setError(e instanceof ApiError ? e.detail : (e as Error).message);
        }
      } finally {
        setBusy(null);
      }
    },
    [dom, targetDomainId, loadCandidates],
  );

  const decide = useCallback(
    async (
      candidateId: string,
      decision: "APPROVE" | "REJECT",
      kind: "resolution" | "relationship",
    ) => {
      setBusy("decision");
      setError(null);
      setNotice(null);
      try {
        // The server requires a reason on rejection for both kinds.
        const reason =
          decision === "REJECT" ? "Rejected from the cross-source review queue." : null;
        if (kind === "resolution") {
          await decideCrossSourceResolutionCandidate(candidateId, decision, reason);
        } else {
          await decideRelationshipCandidate(candidateId, decision, reason);
        }
        setNotice(`Candidate ${decision === "APPROVE" ? "approved" : "rejected"}.`);
        void loadCandidates();
      } catch (e) {
        setError(e instanceof ApiError ? e.detail : (e as Error).message);
      } finally {
        setBusy(null);
      }
    },
    [loadCandidates],
  );

  const otherDomains = domains.filter((d) => d.id !== dom);
  const pendingCount =
    (candidates ?? []).filter((c) => c.status === "PENDING").length +
    (relationships ?? []).filter((c) => c.status === "PENDING").length;

  return (
    <div className="xs">
      <header className="xs__head">
        <div>
          <h1 className="xs__h1">Cross-source</h1>
          <p className="xs__lede">
            The two questions a single data source cannot answer: does this column point into
            another system, and are these two tables the same object? Scoped by data domain,
            because that is the boundary permission is granted across.
          </p>
        </div>
        <div className="xs__stats">
          <span>
            <b className="tnum">{domainDatasourceIds.length}</b> sources in scope
          </span>
          <span>
            <b className="tnum">{pendingCount}</b> pending decisions
          </span>
        </div>
      </header>

      <div className="xs__controls">
        <Field label="Data domain">
          <select
            value={dom ?? ""}
            aria-label="Data domain"
            onChange={(e) => setParams({ dom: e.target.value || null })}
          >
            <option value="">Select a domain…</option>
            {domains.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Also pair against">
          <select
            value={targetDomainId}
            aria-label="Also pair against"
            disabled={!dom}
            onChange={(e) => setTargetDomainId(e.target.value)}
          >
            <option value="">This domain only</option>
            {otherDomains.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Status">
          <select
            value={statusFilter}
            aria-label="Status"
            onChange={(e) => setParams({ status: e.target.value })}
          >
            <option value="PENDING">Pending</option>
            <option value="APPROVED">Approved</option>
            <option value="REJECTED">Rejected</option>
            <option value="ALL">All</option>
          </select>
        </Field>
        <div className="xs__spacer" />
        <Button
          variant="primary"
          disabled={!dom || busy !== null}
          onClick={() => void runDiscovery("relationships")}
          title="Infer column relationships across the data sources in this domain"
        >
          {busy === "relationships" ? "Scanning…" : "Find relationships"}
        </Button>
        <Button
          disabled={!dom || busy !== null}
          onClick={() => void runDiscovery("resolutions")}
          title="Find tables in different sources that describe the same real-world object"
        >
          {busy === "resolutions" ? "Scanning…" : "Find same-object tables"}
        </Button>
      </div>

      <p className="xs__note">
        Pairing sources inside one domain is free. Pairing across into another domain needs an
        active cross-boundary grant — scanning without one is refused, not silently narrowed.
      </p>

      {notice ? (
        <div className="xs__notice" role="status">
          {notice}
        </div>
      ) : null}
      {error ? (
        <div className="xs__error" role="alert">
          {error}
        </div>
      ) : null}

      {dom ? (
        <CrossBoundaryGrants
          domainId={dom}
          domains={domains}
          suggestedSourceDomainId={grantTargetDomainId}
        />
      ) : null}

      <section className="xs__results">
        <div className="xs__sub">Cross-source relationships</div>
        {!dom ? null : relationships === null ? (
          <div className="xs__load" role="status">
            Loading relationships…
          </div>
        ) : relationships.length === 0 ? (
          <Empty
            title="No cross-source relationships"
            hint="Run a scan above. Same-source candidates are reviewed on the Relationships screen."
          />
        ) : (
          <ol className="xs__list">
            {relationships.map((candidate) => (
              <RelationshipRow
                key={candidate.id}
                candidate={candidate}
                datasources={datasources}
                onDecide={(id, decision) => void decide(id, decision, "relationship")}
                busy={busy !== null}
              />
            ))}
          </ol>
        )}
      </section>

      <section className="xs__results">
        <div className="xs__sub">Same-object candidates</div>
        {!dom ? (
          <Empty
            title="Select a data domain"
            hint="Cross-source work is scoped by domain, because that is the boundary permission is granted across."
          />
        ) : candidates === null ? (
          <div className="xs__load" role="status">
            Loading candidates…
          </div>
        ) : candidates.length === 0 ? (
          <Empty
            title="No candidates"
            hint="Run a scan above, or change the status filter to see decided ones."
          />
        ) : (
          <ol className="xs__list">
            {candidates.map((candidate) => (
              <CandidateRow
                key={candidate.id}
                candidate={candidate}
                datasources={datasources}
                onDecide={(id, decision) => void decide(id, decision, "resolution")}
                busy={busy !== null}
              />
            ))}
          </ol>
        )}
      </section>

      {error && !dom ? <ErrorState detail={error} onRetry={() => void loadCandidates()} /> : null}
    </div>
  );
}
