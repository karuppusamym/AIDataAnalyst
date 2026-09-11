import { useCallback, useEffect, useMemo, useState } from "react";
import {
  bulkDecideParsedLineageEdges,
  decideParsedLineageEdge,
  listParsedLineageReviewQueue,
} from "../lib/api";
import type { ParsedLineageEdgeReviewQueueItemRead } from "../lib/types";
import type {
  ParsedLineageEdgeDecision,
  ParsedLineageEdgeType,
} from "../lib/ui-types";
import {
  Button,
  CopyLinkButton,
  Empty,
  ErrorState,
  Field,
  Pill,
} from "../components/primitives";
/* T18: the same review-detail shell the governance queue decides inside. The
   edge-type rules, the five parser tables and the bulk semantics below are
   untouched -- what this screen gains is the detail every review type owes a
   reviewer, and a lost race presented as the other decision rather than as an
   error string. */
import {
  ReviewDetailShell,
  conflictFromError,
  type ReviewConflict,
} from "../components/ReviewDetail";
import { useUrlState } from "../lib/useUrlState";

/* ---------------------------------------------------------------------------
   P1-05 / ADR-0026 — parsed-lineage-edge review queue.

   First-cut, functional table view of PROPOSED lineage edges across the
   non-governed parser-produced edge tables (view / procedure SQL / captured
   routine / dbt-column / OpenLineage-table / OpenLineage-column). Approve / Reject
   post to the same maker-checker endpoint the RelationshipCandidate
   review flow uses. Single and bulk decisions require a reason; the queue
   supports pagination and filtering by edge type and confidence.
--------------------------------------------------------------------------- */

const EDGE_TYPES: ParsedLineageEdgeType[] = [
  "VIEW",
  "PROCEDURE",
  "ROUTINE",
  "DBT",
  "OPENLINEAGE_TABLE",
  "OPENLINEAGE_COLUMN",
];

const CONFIDENCE_STRING_TO_FLOAT: Record<string, number> = {
  FULL: 1.0,
  PARTIAL: 0.6,
  LOW: 0.3,
};

function confidenceDisplay(raw: string | number | null): string {
  if (raw == null) return "—";
  if (typeof raw === "number") return raw.toFixed(2);
  return raw;
}

function confidenceFloat(raw: string | number | null): number | null {
  if (raw == null) return null;
  if (typeof raw === "number") return raw;
  const key = String(raw).toUpperCase();
  return CONFIDENCE_STRING_TO_FLOAT[key] ?? null;
}

/** One edge's stable key across the parser tables. `edge_id` alone is not
 *  unique: each table has its own id space, which is why every selection in
 *  this screen is `${edge_type}:${edge_id}`. */
const edgeKey = (item: { edge_type: string; edge_id: string }) =>
  `${item.edge_type}:${item.edge_id}`;

export function ParsedLineageReviewScreen() {
  const [params, setParams] = useUrlState();
  /* The focused edge lives in the URL, so the pane a reviewer is looking at is
   * shareable and survives Back/Forward -- `parsed-lineage-review` already
   * declares `review` in `SCREEN_QUERY_FIELDS`; nothing on this screen read it. */
  const focusedKey = params.get("review");
  const [items, setItems] = useState<ParsedLineageEdgeReviewQueueItemRead[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [reason, setReason] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /* The edge-type filter lives in the URL too, so a link -- the lineage
     agent's "Open in review queue" -- can open the queue already filtered. */
  const typeParam = params.get("type");
  const edgeType: ParsedLineageEdgeType | "" = EDGE_TYPES.includes(
    typeParam as ParsedLineageEdgeType,
  )
    ? (typeParam as ParsedLineageEdgeType)
    : "";
  const [minConfidence, setMinConfidence] = useState<string>("");
  const [inflight, setInflight] = useState<string | null>(null);
  const [ackMessage, setAckMessage] = useState<string | null>(null);
  /* A refusal about an edge's own review state (already decided, or the maker
     trying to check their own edge) is not a load failure and does not belong
     in the screen's error strip, which is what it used to get: `setError` there
     put "parsed lineage edge is already approved" where "Could not load queue"
     goes. It belongs on the edge, in the shared conflict panel. */
  const [conflicts, setConflicts] = useState<Record<string, ReviewConflict>>({});

  const load = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setError(null);
      try {
        const parsed = minConfidence ? Number(minConfidence) : null;
        const result = await listParsedLineageReviewQueue(
          {
            edgeType: edgeType || null,
            minConfidence: parsed != null && !Number.isNaN(parsed) ? parsed : null,
            limit: 100,
            offset,
          },
          signal,
        );
        setItems(result.items);
        setSelected(new Set());
        setTotal(result.total);
      } catch (err) {
        if ((err as { name?: string })?.name === "AbortError") return;
        setError(err instanceof Error ? err.message : "Failed to load review queue");
      } finally {
        setLoading(false);
      }
    },
    [edgeType, minConfidence, offset],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const decide = useCallback(
    async (
      item: ParsedLineageEdgeReviewQueueItemRead,
      decision: ParsedLineageEdgeDecision,
      decisionReason: string,
    ) => {
      if (!decisionReason.trim()) return;
      const key = edgeKey(item);
      setInflight(item.edge_id);
      setConflicts((current) => {
        if (!(key in current)) return current;
        const next = { ...current };
        delete next[key];
        return next;
      });
      try {
        await decideParsedLineageEdge(item.edge_id, {
          edge_type: item.edge_type,
          decision,
          reason: decisionReason.trim(),
        });
        setAckMessage(
          `${item.edge_type} edge ${decision === "APPROVED" ? "approved" : "rejected"}.`,
        );
        await load();
      } catch (err) {
        const conflict = conflictFromError(err);
        if (conflict) {
          setConflicts((current) => ({ ...current, [key]: conflict }));
          setParams({ review: key });
          await load();
        } else {
          setError(err instanceof Error ? err.message : "Decision failed");
        }
      } finally {
        setInflight(null);
      }
    },
    [load, setParams],
  );

  const bulkDecide = async (decision: ParsedLineageEdgeDecision) => {
    if (!reason.trim() || !selected.size) return;
    setInflight("bulk");
    setError(null);
    try {
      const result = await bulkDecideParsedLineageEdges({
        items: items.filter(item => selected.has(`${item.edge_type}:${item.edge_id}`))
          .map(({edge_type, edge_id}) => ({edge_type, edge_id})),
        decision, reason: reason.trim(),
      });
      setAckMessage(`${result.succeeded_count} succeeded; ${result.failed_count} failed. ${result.results.filter(row => row.status === "FAILED").map(row => `${row.edge_type} ${row.edge_id}: ${row.reason ?? "Decision refused"}`).join("; ")}`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Bulk decision failed");
    } finally { setInflight(null); }
  };

  const summary = useMemo(
    () => `${items.length} shown of ${total} total PROPOSED edges`,
    [items.length, total],
  );

  const focused = useMemo(
    () => items.find((item) => edgeKey(item) === focusedKey) ?? null,
    [items, focusedKey],
  );

  return (
    <section aria-labelledby="parsed-lineage-review-title" className="parsed-review">
      <header style={{ marginBottom: "1rem" }}>
        <h1 id="parsed-lineage-review-title">Parsed lineage review</h1>
        <p style={{ maxWidth: "60ch" }}>
          PROPOSED lineage edges from the non-governed parsers — view,
          procedure SQL, captured routine, dbt, OpenLineage table, OpenLineage
          column. Approve to fold into the shared graph; reject to keep out and
          record why.
          Maker-checker enforced: you cannot decide an edge you created.
        </p>
      </header>

      <div
        style={{ display: "flex", gap: "0.5rem", alignItems: "end", flexWrap: "wrap", marginBottom: "0.75rem" }}
      >
        <Field label="Edge type">
          <select
            value={edgeType}
            onChange={(event) => { setOffset(0); setParams({ type: event.target.value || null }); }}
          >
            <option value="">All</option>
            {EDGE_TYPES.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Min confidence">
          <input
            type="number"
            min={0}
            max={1}
            step={0.05}
            value={minConfidence}
            onChange={(event) => { setOffset(0); setMinConfidence(event.target.value); }}
            placeholder="0.0 - 1.0"
          />
        </Field>
        <Button onClick={() => void load()} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </Button>
      </div>

      {ackMessage ? (
        <div role="status" style={{ marginBottom: "0.75rem" }}>
          {ackMessage}
        </div>
      ) : null}

      {error ? <ErrorState title="Could not load queue" detail={error} onRetry={() => void load()} /> : null}

      {!loading && items.length === 0 && !error ? (
        <Empty
          title="No proposed lineage edges"
          hint="Nothing waiting for review right now."
        />
      ) : null}

      {items.length > 0 ? (
        <>
          <div style={{ margin: "0.5rem 0" }}>{summary}</div>
          <div className="parsed-review__actions">
            <Field label="Decision reason"><input value={reason} onChange={event => setReason(event.target.value)} placeholder="Explain the review decision" /></Field>
            <Button disabled={!selected.size || !reason.trim() || !!inflight || loading} onClick={() => void bulkDecide("APPROVED")}>Approve selected ({selected.size})</Button>
            <Button disabled={!selected.size || !reason.trim() || !!inflight || loading} onClick={() => void bulkDecide("REJECTED")}>Reject selected ({selected.size})</Button>
          </div>
          <div className="parsed-review__table" tabIndex={0} role="region" aria-label="Proposed lineage edges">
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ textAlign: "left" }}>
                <th><input type="checkbox" aria-label="Select all on this page" checked={items.length > 0 && selected.size === items.length} onChange={event => setSelected(event.target.checked ? new Set(items.map(item => `${item.edge_type}:${item.edge_id}`)) : new Set())} /></th>
                <th>Type</th>
                <th>Source</th>
                <th>Target</th>
                <th>Transformation</th>
                <th>Confidence</th>
                <th>Author</th>
                <th>Source SQL</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => {
                const confidenceHint = confidenceFloat(item.confidence);
                return (
                  <tr key={`${item.edge_type}:${item.edge_id}`} style={{ borderTop: "1px solid #eee" }}>
                    <td><input type="checkbox" aria-label={`Select ${item.source_label} to ${item.target_label}`} checked={selected.has(`${item.edge_type}:${item.edge_id}`)} onChange={event => setSelected(previous => { const next = new Set(previous); const key = `${item.edge_type}:${item.edge_id}`; if(event.target.checked) next.add(key); else next.delete(key); return next; })} /></td>
                    <td>
                      <Pill>{item.edge_type}</Pill>
                    </td>
                    <td>
                      {/* Opens the shared review detail. The table stays the
                          queue; the decision happens with the evidence in
                          front of the reviewer (T18). */}
                      <button
                        type="button"
                        className="parsed-review__open"
                        onClick={() => setParams({ review: edgeKey(item) })}
                      >
                        <code>{item.source_label}</code>
                      </button>
                    </td>
                    <td>
                      <code>{item.target_label}</code>
                    </td>
                    <td>{item.transformation_type ?? "—"}</td>
                    <td
                      title={
                        confidenceHint != null
                          ? `Coerced to ${confidenceHint.toFixed(2)}`
                          : undefined
                      }
                    >
                      {confidenceDisplay(item.confidence)}
                    </td>
                    <td>{item.created_by ?? "—"}</td>
                    <td>
                      <small>
                        {item.source_sql_reference.kind}
                        {Object.entries(item.source_sql_reference)
                          .filter(([k]) => k !== "kind")
                          .map(([k, v]) => (
                            <div key={k}>
                              {k}: <code>{v}</code>
                            </div>
                          ))}
                      </small>
                    </td>
                    <td>
                      <Button
                        onClick={() => void decide(item, "APPROVED", reason)}
                        disabled={!!inflight || loading || !reason.trim()}
                      >
                        Approve
                      </Button>{" "}
                      <Button
                        onClick={() => void decide(item, "REJECTED", reason)}
                        disabled={!!inflight || loading || !reason.trim()}
                      >
                        Reject
                      </Button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          </div>
        </>
      ) : null}

      <div className="parsed-review__actions" aria-label="Queue pagination">
        <Button disabled={loading || !!inflight || offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>Previous page</Button>
        <span>Page {Math.floor(offset / 100) + 1}</span>
        <Button disabled={loading || !!inflight || offset + 100 >= total} onClick={() => setOffset(offset + 100)}>Next page</Button>
      </div>

      {focused ? (
        <ReviewDetailShell
          label="Parsed lineage edge detail"
          className="parsed-review__detail"
          identity={{
            subject: `${focused.source_label} → ${focused.target_label}`,
            target: `${focused.edge_type} edge · parsed lineage`,
            // Only PROPOSED edges are in this queue: the read model filters on
            // it. Naming the status rather than implying it keeps the shell's
            // vocabulary the same across review types.
            status: "PROPOSED",
            raisedBy: focused.created_by,
            raisedAt: focused.created_at,
            confidence: confidenceFloat(focused.confidence),
          }}
          assignment={{
            /* Parsed lineage edges carry no assignee: the queue is open to any
               reviewer who did not write the edge. The maker-checker rule is
               enforced server-side (409), so it is reported when it bites
               rather than guessed at here from `created_by`, which is not
               necessarily this principal. */
            blockedReason: null,
          }}
          diff={
            <p className="rvd__none">
              Approving adds this edge to the shared lineage graph; rejecting keeps it
              out and records why. Parsed edges have no before/after field diff — the
              edge itself is the change.
            </p>
          }
          impact={
            <p className="rvd__none">
              {/* Every parser here states column pairs except OpenLineage's
                  run-level table edges; view, procedure and routine edges were
                  once described as table-level here too. */}
              {focused.edge_type === "OPENLINEAGE_TABLE"
                ? "A table-level edge. Approving affects table lineage and any impact answer that traverses it."
                : "A column-level edge. Approving affects column lineage and any impact answer that traverses it."}
            </p>
          }
          evidence={
            /* The type-specific slot. Each of the parser tables carries a
               different natural key back to its source SQL, so what establishes
               the edge differs by kind -- that is exactly what the shell must
               not flatten. */
            <dl className="rvd__facts">
              <div>
                <dt>Edge type</dt>
                <dd>{focused.edge_type}</dd>
              </div>
              <div>
                <dt>Transformation</dt>
                <dd>{focused.transformation_type ?? "not recorded"}</dd>
              </div>
              <div>
                <dt>Parser confidence</dt>
                <dd>
                  {confidenceDisplay(focused.confidence)}
                  {typeof focused.confidence === "string" &&
                  confidenceFloat(focused.confidence) !== null
                    ? ` (coerced to ${confidenceFloat(focused.confidence)?.toFixed(2)})`
                    : confidenceFloat(focused.confidence) === null
                      ? " — this parser reports no comparable confidence"
                      : ""}
                </dd>
              </div>
              {Object.entries(focused.source_sql_reference).map(([key, value]) => (
                <div key={key}>
                  <dt>{key === "kind" ? "Source" : key.replace(/_/g, " ")}</dt>
                  <dd>
                    <code>{value}</code>
                  </dd>
                </div>
              ))}
            </dl>
          }
          decision={{
            busy: inflight === focused.edge_id,
            error: null,
            /* This endpoint records a rationale for BOTH verdicts, unlike the
               governance queue, which requires one only for a rejection. */
            reasonRequiredFor: ["APPROVE", "REJECT"],
            approveLabel: "Approve edge",
            rejectLabel: "Reject edge",
            onDecide: (verdict, decisionReason) =>
              void decide(
                focused,
                verdict === "APPROVE" ? "APPROVED" : "REJECTED",
                decisionReason ?? "",
              ),
          }}
          conflict={conflicts[edgeKey(focused)] ?? null}
          onRefresh={() => void load()}
          onDismissConflict={() =>
            setConflicts((current) => {
              const next = { ...current };
              delete next[edgeKey(focused)];
              return next;
            })
          }
          onClose={() => setParams({ review: null })}
          footer={
            <CopyLinkButton
              target={{
                screen: "parsed-lineage-review",
                params: { review: edgeKey(focused) },
              }}
              label="Copy permalink"
            />
          }
        />
      ) : null}
    </section>
  );
}
